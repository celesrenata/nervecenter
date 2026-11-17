"""Step builders for the kube agent's runbooks (NS-ADR-120). Pure: nothing runs here.

Every builder returns a list of plan-step dicts (``connector``, ``purpose``, ``args``/``stdin``,
``kind``, ``requires``, ``on_failure_of``, ``rollback``) whose indexes are absolute: ``start`` is
the
index the first returned step will have in the final plan, so builders can be concatenated.
Manifests are built as dicts and written with ``yaml.safe_dump``; names are checked against
DNS-1123 first. Database credentials are passed to Jobs only as ``secretKeyRef`` by secret name,
never read. Backups go to an inline ``nfs:`` volume (no PV or PVC is created).

Chain of a backup: dump Job (backup) -> wait (backup) -> verify Job (change) -> wait
(backup_verify). The last step returned by ``backup_steps`` is the ``backup_verify`` that data
steps ``require``. A repair is: check (read, may fail) -> repair (runs only when the check
failed, ``on_failure_of``) -> restore from the verified backup (runs only when the repair
failed) -> final check.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import yaml

from backend.kube.settings import DNS_LABEL

DBTYPES = ("mysql", "postgres", "redis")
IMAGES = {"mysql": "mariadb:11", "postgres": "postgres:16", "redis": "redis:7"}
TOOLS_IMAGE = "busybox:1.36"
LONGHORN_NS = "longhorn-system"
LABEL = {"app.kubernetes.io/managed-by": "nervecenter-kube"}
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_STAMP = re.compile(r"^\d{14}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_:@-]{0,299}$")
_PWD_ENV = {"mysql": "MYSQL_PWD", "postgres": "PGPASSWORD", "redis": "REDISCLI_AUTH"}
_USER = {"mysql": "root", "postgres": "postgres", "redis": ""}
_EXT = {"mysql": "sql.gz", "postgres": "sql.gz", "redis": "rdb"}
_RUN_AS = {"mysql": 999, "postgres": 999}


def new_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value) or len(value) > 253:
        raise ValueError(f"{what}: not a valid Kubernetes name")
    return value


def _ns(value: Any) -> str:
    if not isinstance(value, str) or not DNS_LABEL.match(value):
        raise ValueError("namespace: not a valid namespace name")
    return value


def _dbtype(value: Any) -> str:
    if value not in DBTYPES:
        raise ValueError(f"dbtype must be one of {', '.join(DBTYPES)}")
    return value


def _stamp(value: str | None) -> str:
    stamp = value or new_stamp()
    if not _STAMP.match(stamp):
        raise ValueError("stamp must be 14 digits")
    return stamp


def _image(value: str) -> str:
    if not _IMAGE.match(value):
        raise ValueError("image: not a valid image reference")
    return value


def nfs_dir(settings: dict[str, Any], dbtype: str) -> str:
    """``<base>/<dbtype>/backups`` on the NAS (``/volume1/Kubernetes/<dbtype>/backups``)."""
    base = str(settings["backup_nfs_base"]).rstrip("/")
    return f"{base}/{dbtype}/backups"


def nfs_dir_note(settings: dict[str, Any], dbtype: str) -> str:
    """Plan text for when no ssh connector can create the backup directory."""
    return (f"The owner must create {nfs_dir(settings, dbtype)} on {settings['backup_nfs_server']} "
            "before the backup steps run.")


class _Steps:
    def __init__(self, start: int) -> None:
        self.start = start
        self.steps: list[dict[str, Any]] = []

    def next_index(self) -> int:
        return self.start + len(self.steps)

    def add(self, connector: str, purpose: str, *, kind: str, args: list[str] | None = None,
            command: str | None = None, stdin: str | None = None,
            requires: list[int] | None = None, on_failure_of: list[int] | None = None,
            rollback: str | None = None) -> int:
        step: dict[str, Any] = {"connector": connector, "purpose": purpose[:300], "kind": kind}
        if args is not None:
            step["args"] = args
        if command is not None:
            step["command"] = command
        if stdin is not None:
            step["stdin"] = stdin
        if requires:
            step["requires"] = sorted(set(requires))
        if on_failure_of:
            step["on_failure_of"] = sorted(set(on_failure_of))
        if rollback:
            step["rollback"] = rollback
        self.steps.append(step)
        return self.start + len(self.steps) - 1


def _dump(manifest: dict[str, Any]) -> str:
    return yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False)


def _script(lines: list[str], *, bash: bool) -> list[str]:
    head = "set -eu" + ("; set -o pipefail" if bash else "")
    return ["bash" if bash else "sh", "-c", "\n".join([head, *lines])]


def _job(name: str, namespace: str, image: str, command: list[str], settings: dict[str, Any],
         dbtype: str, *, nfs_read_only: bool = False, env: list[dict[str, Any]] | None = None,
         run_as: int | None = None, pvc: str | None = None, pvc_mount: str = "/data",
         pvc_read_only: bool = True) -> dict[str, Any]:
    mounts = [{"name": "backups", "mountPath": "/backups", "readOnly": nfs_read_only}]
    volumes: list[dict[str, Any]] = [{"name": "backups", "nfs": {
        "server": settings["backup_nfs_server"], "path": nfs_dir(settings, dbtype),
        "readOnly": nfs_read_only}}]
    if pvc:
        mounts.append({"name": "data", "mountPath": pvc_mount, "readOnly": pvc_read_only})
        volumes.append({"name": "data", "persistentVolumeClaim": {
            "claimName": _name(pvc, "pvc"), "readOnly": pvc_read_only}})
    container: dict[str, Any] = {"name": "job", "image": _image(image), "command": command,
                                 "volumeMounts": mounts}
    if env:
        container["env"] = env
    pod_spec: dict[str, Any] = {"restartPolicy": "Never", "containers": [container],
                                "volumes": volumes}
    if run_as is not None:
        pod_spec["securityContext"] = {"runAsUser": run_as, "runAsNonRoot": True}
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(LABEL)},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 86400,
                 "activeDeadlineSeconds": 3600,
                 "template": {"metadata": {"labels": dict(LABEL)}, "spec": pod_spec}},
    }


def _env(dbtype: str, host: str, user: str | None, secret_name: str | None,
         secret_key: str) -> list[dict[str, Any]]:
    env: list[dict[str, Any]] = [{"name": "DB_HOST", "value": _name(host, "host")},
                                 {"name": "DB_USER", "value": user or _USER[dbtype]}]
    if secret_name:
        env.append({"name": _PWD_ENV[dbtype], "valueFrom": {"secretKeyRef": {
            "name": _name(secret_name, "secret_name"), "key": _name(secret_key, "secret_key")}}})
    return env


def _job_name(prefix: str, dbtype: str, stamp: str) -> str:
    return f"nc-{prefix}-{dbtype}-{stamp}"


def _apply(b: _Steps, connector: str, purpose: str, manifest: dict[str, Any], *, kind: str,
           requires: list[int] | None = None, on_failure_of: list[int] | None = None,
           rollback: str | None = None) -> int:
    ns = manifest["metadata"]["namespace"]
    return b.add(connector, purpose, kind=kind, args=["apply", "-n", ns, "-f", "-"],
                 stdin=_dump(manifest), requires=requires, on_failure_of=on_failure_of,
                 rollback=rollback)


def _wait(b: _Steps, connector: str, purpose: str, job: str, ns: str, *, kind: str,
          requires: list[int] | None = None, on_failure_of: list[int] | None = None,
          timeout: int = 900) -> int:
    return b.add(connector, purpose, kind=kind, requires=requires, on_failure_of=on_failure_of,
                 args=["wait", "--for=condition=complete", f"job/{job}", "-n", ns,
                       f"--timeout={timeout}s"])


def _dump_script(dbtype: str, file: str) -> list[str]:
    out = f"/backups/{file}"
    if dbtype == "mysql":
        return [f'mariadb-dump -h "$DB_HOST" -u "$DB_USER" --single-transaction --routines '
                f'--events --all-databases | gzip > {out}', f"test -s {out}"]
    if dbtype == "postgres":
        return [f'pg_dumpall -h "$DB_HOST" -U "$DB_USER" --clean --if-exists | gzip > {out}',
                f"test -s {out}"]
    return [f'redis-cli -h "$DB_HOST" --rdb {out}', f"test -s {out}"]


def _verify_script(dbtype: str, file: str) -> list[str]:
    src = f"/backups/{file}"
    if dbtype == "mysql":
        return [
            "mkdir -p /tmp/scratch",
            "mariadb-install-db --no-defaults --datadir=/tmp/scratch "
            "--auth-root-authentication-method=normal > /dev/null",
            "mariadbd --no-defaults --datadir=/tmp/scratch --socket=/tmp/s.sock "
            "--skip-networking &",
            "for i in $(seq 60); do mariadb-admin --no-defaults -S /tmp/s.sock ping && break; "
            "sleep 1; done",
            f"gzip -dc {src} | mariadb --no-defaults -S /tmp/s.sock -u root",
            "mariadb-check --no-defaults -S /tmp/s.sock -u root --all-databases",
        ]
    if dbtype == "postgres":
        return [
            "initdb -D /tmp/scratch -U postgres > /dev/null",
            "pg_ctl -D /tmp/scratch -o \"-k /tmp -c listen_addresses=''\" -w start > /dev/null",
            f"gzip -dc {src} | psql -h /tmp -U postgres -d postgres -q > /dev/null",
            "pg_amcheck -h /tmp -U postgres --all",
            "pg_ctl -D /tmp/scratch stop > /dev/null",
        ]
    return [f"redis-check-rdb {src}"]


def backup_steps(dbtype: str, namespace: str, workload: str, settings: dict[str, Any], *,
                 connector: str, ssh_connector: str | None = None, start: int = 0,
                 stamp: str | None = None, host: str | None = None, user: str | None = None,
                 secret_name: str | None = None, secret_key: str = "password",
                 image: str | None = None) -> list[dict[str, Any]]:
    """Dump to NFS, then restore the dump into a scratch instance and run the db's own check.

    ``host`` defaults to ``workload`` (the database's Service name). The last step returned is
    the ``backup_verify`` step. Without ``ssh_connector`` the plan must say the owner creates the
    directory (see ``nfs_dir_note``).
    """
    dbtype, ns, stamp = _dbtype(dbtype), _ns(namespace), _stamp(stamp)
    host = _name(host or workload, "workload")
    image = image or IMAGES[dbtype]
    file = f"{dbtype}-{stamp}.{_EXT[dbtype]}"
    b = _Steps(start)
    mk = None
    if ssh_connector:
        mk = b.add(ssh_connector, f"Create {nfs_dir(settings, dbtype)} on the NAS", kind="change",
                   command=f"mkdir -p {nfs_dir(settings, dbtype)}",
                   rollback="Leave the empty directory in place.")
    env = _env(dbtype, host, user, secret_name, secret_key)
    dump_name = _job_name("dump", dbtype, stamp)
    dump = _job(dump_name, ns, image, _script(_dump_script(dbtype, file), bash=True), settings,
                dbtype, env=env, run_as=_RUN_AS.get(dbtype))
    d = _apply(b, connector, f"Start the {dbtype} dump Job {dump_name} in {ns}", dump,
               kind="backup", requires=[mk] if mk is not None else None,
               rollback=f"kubectl delete job {dump_name} -n {ns}")
    dw = _wait(b, connector, f"Wait for the dump Job {dump_name}", dump_name, ns, kind="backup",
               requires=[d])
    ver_name = _job_name("verify", dbtype, stamp)
    verify = _job(ver_name, ns, image, _script(_verify_script(dbtype, file), bash=True),
                  settings, dbtype, nfs_read_only=True, run_as=_RUN_AS.get(dbtype))
    v = _apply(b, connector, f"Start the Job {ver_name} that restores the dump into a scratch "
               "instance and checks it", verify, kind="change", requires=[dw],
               rollback=f"kubectl delete job {ver_name} -n {ns}")
    _wait(b, connector, f"Wait for the verification Job {ver_name}", ver_name, ns,
          kind="backup_verify", requires=[v])
    return b.steps


def prune_steps(dbtype: str, namespace: str, settings: dict[str, Any], *, connector: str,
                start: int = 0, stamp: str | None = None) -> list[dict[str, Any]]:
    """Keep ``retention_daily`` days of dumps plus Sunday dumps for ``retention_weekly`` weeks."""
    dbtype, ns, stamp = _dbtype(dbtype), _ns(namespace), _stamp(stamp)
    daily, weekly = int(settings["retention_daily"]), int(settings["retention_weekly"])
    horizon = daily + 7 * weekly
    pattern = f"{dbtype}-*"
    script = [
        "cd /backups",
        f"find . -maxdepth 1 -type f -name '{pattern}' -mtime +{daily - 1} | while read -r f; do",
        '  d=${f#./' + dbtype + '-}; day=$(echo "$d" | cut -c1-8)',
        '  dow=$(date -u -d "$(echo "$day" | cut -c1-4)-$(echo "$day" | cut -c5-6)-'
        '$(echo "$day" | cut -c7-8)" +%u 2>/dev/null || echo 0)',
        f'  if [ "$dow" = 7 ] && [ -n "$(find "$f" -mtime -{horizon})" ]; then continue; fi',
        '  rm -f "$f"',
        "done",
    ]
    name = _job_name("prune", dbtype, stamp)
    job = _job(name, ns, TOOLS_IMAGE, _script(script, bash=False), settings, dbtype)
    b = _Steps(start)
    a = _apply(b, connector, f"Start the prune Job {name}: keep {daily} daily and {weekly} "
               "weekly dumps", job, kind="prune",
               rollback="Deleted dumps cannot be restored; the NAS snapshot is the only copy.")
    _wait(b, connector, f"Wait for the prune Job {name}", name, ns, kind="prune", requires=[a],
          timeout=300)
    return b.steps


def restore_steps(dbtype: str, namespace: str, workload: str, settings: dict[str, Any], *,
                  connector: str, stamp: str, verified_by: int,
                  on_failure_of: list[int] | None = None, start: int = 0,
                  host: str | None = None, user: str | None = None,
                  secret_name: str | None = None, secret_key: str = "password",
                  image: str | None = None, data_pvc: str | None = None,
                  workload_kind: str = "deployment", replicas: int = 1) -> list[dict[str, Any]]:
    """Load the dump of ``stamp`` back. Needs the index of the verified backup step.

    Redis needs ``data_pvc``: the workload is scaled to 0, the RDB copied into the PVC and the
    workload scaled back to ``replicas``.
    """
    dbtype, ns, stamp = _dbtype(dbtype), _ns(namespace), _stamp(stamp)
    host = _name(host or workload, "workload")
    image = image or IMAGES[dbtype]
    file = f"{dbtype}-{stamp}.{_EXT[dbtype]}"
    src = f"/backups/{file}"
    b = _Steps(start)
    kw = {"kind": "restore", "requires": [verified_by], "on_failure_of": on_failure_of}
    name = _job_name("restore", dbtype, stamp)
    if dbtype == "redis":
        if not data_pvc:
            raise ValueError("redis restore needs data_pvc")
        target = f"{_name(workload_kind, 'workload_kind')}/{_name(workload, 'workload')}"
        b.add(connector, f"Scale {target} to 0 before the restore", args=[
            "scale", target, "--replicas=0", "-n", ns],
            rollback=f"kubectl scale {target} --replicas={replicas} -n {ns}", **kw)
        script = ["rm -rf /data/appendonlydir /data/appendonly.aof", f"cp {src} /data/dump.rdb"]
        job = _job(name, ns, TOOLS_IMAGE, _script(script, bash=False), settings, dbtype,
                   nfs_read_only=True, pvc=data_pvc, pvc_read_only=False)
        _apply(b, connector, f"Copy {file} into the PVC with Job {name}", job,
               rollback=f"kubectl delete job {name} -n {ns}", **kw)
        _wait(b, connector, f"Wait for the restore Job {name}", name, ns, **kw)
        b.add(connector, f"Scale {target} back to {replicas}", args=[
            "scale", target, f"--replicas={int(replicas)}", "-n", ns], **kw)
        return b.steps
    env = _env(dbtype, host, user, secret_name, secret_key)
    if dbtype == "mysql":
        line = f'gzip -dc {src} | mariadb -h "$DB_HOST" -u "$DB_USER"'
    else:
        line = f'gzip -dc {src} | psql -h "$DB_HOST" -U "$DB_USER" -d postgres -q > /dev/null'
    job = _job(name, ns, image, _script([line], bash=True), settings, dbtype,
               nfs_read_only=True, env=env, run_as=_RUN_AS.get(dbtype))
    _apply(b, connector, f"Restore {file} into {host} with Job {name}", job,
           rollback=f"kubectl delete job {name} -n {ns}", **kw)
    _wait(b, connector, f"Wait for the restore Job {name}", name, ns, **kw)
    return b.steps


def _backup_or_given(builder: _Steps, dbtype: str, namespace: str, workload: str,
                     settings: dict[str, Any], connector: str, ssh_connector: str | None,
                     stamp: str, verified_by: int | None, **backup_kw: Any) -> int:
    if verified_by is not None:
        return verified_by
    only = {k: v for k, v in backup_kw.items()
            if k in ("host", "user", "secret_name", "secret_key", "image")}
    builder.steps.extend(backup_steps(
        dbtype, namespace, workload, settings, connector=connector, ssh_connector=ssh_connector,
        start=builder.next_index(), stamp=stamp, **only))
    return builder.next_index() - 1


def _exec(pod: str, ns: str, argv: list[str]) -> list[str]:
    return ["exec", "-n", ns, _name(pod, "pod"), "--", *argv]


def mysql_repair_steps(namespace: str, pod: str, workload: str, settings: dict[str, Any], *,
                       connector: str, ssh_connector: str | None = None, start: int = 0,
                       verified_by: int | None = None, stamp: str | None = None,
                       **backup_kw: Any) -> list[dict[str, Any]]:
    """Backup (unless ``verified_by`` is given), check, repair, restore fallback, final check.

    ``mysqlcheck`` runs in ``pod`` with the credentials the pod already has.
    """
    ns, stamp = _ns(namespace), _stamp(stamp)
    b = _Steps(start)
    ver = _backup_or_given(b, "mysql", ns, workload, settings, connector, ssh_connector, stamp,
                           verified_by, **backup_kw)
    check = b.add(connector, f"Check all tables in {pod} (read only)", kind="read",
                  args=_exec(pod, ns, ["mysqlcheck", "--check", "--all-databases"]))
    repair = b.add(connector, "Repair damaged tables (runs only when the check failed)",
                   kind="repair", requires=[ver], on_failure_of=[check],
                   args=_exec(pod, ns, ["mysqlcheck", "--auto-repair", "--all-databases"]),
                   rollback="Restore the verified dump (the next steps do this if repair fails).")
    b.steps.extend(restore_steps("mysql", ns, workload, settings, connector=connector,
                                 stamp=stamp, verified_by=ver, on_failure_of=[repair],
                                 start=b.next_index(), **_restore_kw(backup_kw)))
    b.add(connector, "Check all tables again (read only)", kind="read",
          args=_exec(pod, ns, ["mysqlcheck", "--check", "--all-databases"]))
    return b.steps


def _restore_kw(backup_kw: dict[str, Any]) -> dict[str, Any]:
    keys = ("host", "user", "secret_name", "secret_key", "image", "data_pvc", "workload_kind",
            "replicas")
    return {k: v for k, v in backup_kw.items() if k in keys}


def redis_repair_steps(namespace: str, pod: str, workload: str, settings: dict[str, Any], *,
                       connector: str, data_pvc: str, ssh_connector: str | None = None,
                       start: int = 0, verified_by: int | None = None, stamp: str | None = None,
                       aof_path: str = "/data/appendonlydir/appendonly.aof.manifest",
                       **backup_kw: Any) -> list[dict[str, Any]]:
    """Backup, ``redis-check-aof``, ``--fix`` when the check failed, RDB restore fallback."""
    ns, stamp = _ns(namespace), _stamp(stamp)
    if not re.match(r"^/[A-Za-z0-9._/-]{1,200}$", aof_path):
        raise ValueError("aof_path: not a plain absolute path")
    b = _Steps(start)
    ver = _backup_or_given(b, "redis", ns, workload, settings, connector, ssh_connector, stamp,
                           verified_by, **backup_kw)
    check = b.add(connector, "Check the append-only file (read only)", kind="read",
                  args=_exec(pod, ns, ["redis-check-aof", aof_path]))
    repair = b.add(connector, "Truncate the damaged AOF tail (runs only when the check failed)",
                   kind="repair", requires=[ver], on_failure_of=[check],
                   args=_exec(pod, ns, ["redis-check-aof", "--fix", aof_path]),
                   rollback="Restore the verified RDB (the next steps do this if the fix fails).")
    b.steps.extend(restore_steps("redis", ns, workload, settings, connector=connector,
                                 stamp=stamp, verified_by=ver, on_failure_of=[repair],
                                 start=b.next_index(), data_pvc=data_pvc,
                                 **_restore_kw(backup_kw)))
    return b.steps


def postgres_steps(namespace: str, workload: str, settings: dict[str, Any], *, connector: str,
                   ssh_connector: str | None = None, start: int = 0,
                   verified_by: int | None = None, stamp: str | None = None,
                   **backup_kw: Any) -> list[dict[str, Any]]:
    """Backup, ``pg_amcheck`` Job, ``reindexdb`` Job when the check failed, restore fallback."""
    ns, stamp = _ns(namespace), _stamp(stamp)
    host = _name(backup_kw.get("host") or workload, "workload")
    image = backup_kw.get("image") or IMAGES["postgres"]
    env = _env("postgres", host, backup_kw.get("user"), backup_kw.get("secret_name"),
               backup_kw.get("secret_key", "password"))
    b = _Steps(start)
    ver = _backup_or_given(b, "postgres", ns, workload, settings, connector, ssh_connector,
                           stamp, verified_by, **backup_kw)

    def job(prefix: str, line: str) -> dict[str, Any]:
        return _job(_job_name(prefix, "postgres", stamp), ns, image,
                    _script([line], bash=True), settings, "postgres", nfs_read_only=True,
                    env=env, run_as=_RUN_AS["postgres"])

    check_job = job("check", 'pg_amcheck -h "$DB_HOST" -U "$DB_USER" --all')
    ca = _apply(b, connector, "Start the pg_amcheck Job", check_job, kind="change",
                rollback=f"kubectl delete job {_job_name('check', 'postgres', stamp)} -n {ns}")
    cw = _wait(b, connector, "Wait for pg_amcheck (read only)", _job_name("check", "postgres",
               stamp), ns, kind="read", requires=[ca])
    fix_name = _job_name("reindex", "postgres", stamp)
    fix_job = job("reindex", 'reindexdb -h "$DB_HOST" -U "$DB_USER" --all')
    _apply(b, connector, "Start the reindexdb Job (runs only when the check failed)", fix_job,
           kind="change", requires=[ver], on_failure_of=[cw],
           rollback=f"kubectl delete job {fix_name} -n {ns}")
    fw = _wait(b, connector, "Wait for reindexdb (runs only when the check failed)", fix_name,
               ns, kind="repair", requires=[ver], on_failure_of=[cw])
    b.steps.extend(restore_steps("postgres", ns, workload, settings, connector=connector,
                                 stamp=stamp, verified_by=ver, on_failure_of=[fw],
                                 start=b.next_index(), **_restore_kw(backup_kw)))
    final_name = _job_name("final", "postgres", stamp)
    fa = _apply(b, connector, "Start the final pg_amcheck Job", job("final", (
        'pg_amcheck -h "$DB_HOST" -U "$DB_USER" --all')), kind="change",
        rollback=f"kubectl delete job {final_name} -n {ns}")
    _wait(b, connector, "Wait for the final pg_amcheck (read only)", final_name, ns,
          kind="read", requires=[fa])
    return b.steps


def nextcloud_upgrade_steps(namespace: str, pod: str, settings: dict[str, Any], *,
                            connector: str, db_workload: str, config_pvc: str,
                            db_type: str = "mysql", db_namespace: str | None = None,
                            ssh_connector: str | None = None, start: int = 0,
                            stamp: str | None = None, **backup_kw: Any) -> list[dict[str, Any]]:
    """Back up the database and the config PVC, then ``occ upgrade`` and the db: repairs.

    ``occ upgrade`` handles maintenance mode itself; ``occ maintenance:mode`` is not used.
    """
    ns, stamp = _ns(namespace), _stamp(stamp)
    b = _Steps(start)
    db_ver = _backup_or_given(b, db_type, db_namespace or ns, db_workload, settings, connector,
                              ssh_connector, stamp, None, **backup_kw)
    mk = None
    if ssh_connector:
        mk = b.add(ssh_connector, f"Create {nfs_dir(settings, 'nextcloud')} on the NAS",
                   kind="change", command=f"mkdir -p {nfs_dir(settings, 'nextcloud')}",
                   rollback="Leave the empty directory in place.")
    file = f"nextcloud-{stamp}.tar.gz"
    cfg_name, chk_name = _job_name("cfgdump", "nextcloud", stamp), _job_name(
        "cfgverify", "nextcloud", stamp)
    cfg = _job(cfg_name, ns, TOOLS_IMAGE, _script([
        f"tar -czf /backups/{file} -C /data .", f"test -s /backups/{file}"], bash=False),
        settings, "nextcloud", pvc=config_pvc)
    ca = _apply(b, connector, f"Start Job {cfg_name} that archives the Nextcloud config PVC", cfg,
                kind="backup", requires=[mk] if mk is not None else None,
                rollback=f"kubectl delete job {cfg_name} -n {ns}")
    cw = _wait(b, connector, f"Wait for Job {cfg_name}", cfg_name, ns, kind="backup",
               requires=[ca])
    chk = _job(chk_name, ns, TOOLS_IMAGE, _script([f"tar -tzf /backups/{file} > /dev/null"],
               bash=False), settings, "nextcloud", nfs_read_only=True)
    va = _apply(b, connector, f"Start Job {chk_name} that lists the archive", chk, kind="change",
                requires=[cw], rollback=f"kubectl delete job {chk_name} -n {ns}")
    cfg_ver = _wait(b, connector, f"Wait for Job {chk_name}", chk_name, ns, kind="backup_verify",
                    requires=[va], timeout=600)
    prev = b.add(connector, "Run occ upgrade as www-data", kind="change",
                 requires=[db_ver, cfg_ver],
                 args=_exec(pod, ns, ["runuser", "-u", "www-data", "--", "php", "occ", "upgrade"]),
                 rollback="Restore the database dump and the config archive from the NAS.")
    for sub in (["db:add-missing-indices"], ["db:add-missing-columns"],
                ["db:convert-filecache-bigint", "--no-interaction"]):
        prev = b.add(connector, f"Run occ {sub[0]}", kind="change", requires=[prev],
                     args=_exec(pod, ns, ["runuser", "-u", "www-data", "--", "php", "occ", *sub]),
                     rollback="Restore the database dump from the NAS.")
    return b.steps


def crashloop_steps(namespace: str, workload: str, *, connector: str,
                    workload_kind: str = "deployment", start: int = 0) -> list[dict[str, Any]]:
    """Describe the workload, restart it, wait for the rollout."""
    ns = _ns(namespace)
    target = f"{_name(workload_kind, 'workload_kind')}/{_name(workload, 'workload')}"
    b = _Steps(start)
    d = b.add(connector, f"Describe {target}", kind="read", args=["describe", target, "-n", ns])
    r = b.add(connector, f"Restart {target}", kind="change", requires=[d],
              args=["rollout", "restart", target, "-n", ns],
              rollback=f"kubectl rollout undo {target} -n {ns}")
    b.add(connector, f"Wait for the rollout of {target}", kind="read", requires=[r],
          args=["rollout", "status", target, "-n", ns, "--timeout=180s"])
    return b.steps


def imagepull_steps(namespace: str, workload: str, *, connector: str,
                    workload_kind: str = "deployment", container: str | None = None,
                    fixed_image: str | None = None, start: int = 0) -> list[dict[str, Any]]:
    """Show the pull errors, then set a corrected image (or restart) and wait.

    kubectl cannot ask the registry which tags exist; the events carry the registry's answer.
    """
    ns = _ns(namespace)
    target = f"{_name(workload_kind, 'workload_kind')}/{_name(workload, 'workload')}"
    b = _Steps(start)
    d = b.add(connector, f"Describe {target}", kind="read", args=["describe", target, "-n", ns])
    e = b.add(connector, "Show the pull failures reported by the registry", kind="read",
              args=["get", "events", "-n", ns, "--field-selector", "reason=Failed"])
    if fixed_image:
        c = _name(container, "container") if container else None
        if c is None:
            raise ValueError("container is required with fixed_image")
        fix = b.add(connector, f"Set the image of {c} to {fixed_image}", kind="change",
                    requires=[d, e], args=["set", "image", target, f"{c}={_image(fixed_image)}",
                                           "-n", ns],
                    rollback=f"kubectl rollout undo {target} -n {ns}")
    else:
        fix = b.add(connector, f"Restart {target}", kind="change", requires=[d, e],
                    args=["rollout", "restart", target, "-n", ns],
                    rollback=f"kubectl rollout undo {target} -n {ns}")
    b.add(connector, f"Wait for the rollout of {target}", kind="read", requires=[fix],
          args=["rollout", "status", target, "-n", ns, "--timeout=180s"])
    return b.steps


def pvc_steps(namespace: str, pvc: str, volume: str, *, connector: str, start: int = 0,
              stamp: str | None = None, pod: str | None = None,
              delete_pvc: bool = False) -> list[dict[str, Any]]:
    """Longhorn snapshot of ``volume`` first, then optionally delete a stuck pod or the PVC.

    The snapshot lives in ``longhorn-system`` (protected: needs an active override). A PVC is
    deleted only after the snapshot is ready.
    """
    ns, stamp = _ns(namespace), _stamp(stamp)
    pvc, volume = _name(pvc, "pvc"), _name(volume, "volume")
    snap = f"nc-snap-{stamp}-{volume}"[:63].rstrip("-.")
    manifest = {"apiVersion": "longhorn.io/v1beta2", "kind": "Snapshot",
                "metadata": {"name": snap, "namespace": LONGHORN_NS, "labels": dict(LABEL)},
                "spec": {"volume": volume, "createSnapshot": True}}
    b = _Steps(start)
    a = _apply(b, connector, f"Take a Longhorn snapshot of {volume} (backs {pvc})", manifest,
               kind="backup", rollback=f"kubectl delete snapshots.longhorn.io {snap} -n "
               f"{LONGHORN_NS}")
    w = b.add(connector, f"Wait until snapshot {snap} is ready", kind="backup_verify",
              requires=[a], args=["wait", "--for=jsonpath={.status.readyToUse}=true",
                                  f"snapshots.longhorn.io/{snap}", "-n", LONGHORN_NS,
                                  "--timeout=180s"])
    if pod:
        b.add(connector, f"Delete pod {pod} so it remounts {pvc}", kind="change", requires=[w],
              args=["delete", "pod", _name(pod, "pod"), "-n", ns],
              rollback="The workload controller recreates the pod.")
    if delete_pvc:
        b.add(connector, f"Delete PVC {pvc}", kind="change", requires=[w],
              args=["delete", "pvc", pvc, "-n", ns],
              rollback=f"Roll back to snapshot {snap} of volume {volume}.")
    return b.steps
