# Owner item closed: object-store encryption (durable NFS + client-side AES-256-GCM)

Date: 2026-11-12. The owner offered two options: "minio on the cluster (ephemeral), or NFS storage (permanant)". **NFS was chosen**, because the stored mail bodies are the only source for backfill and re-assessment, and an ephemeral store would lose them.

## Before

- NerveCenter objects lived in the shared `minio-crawler` tenant (`minio-service` namespace), reached over plain HTTP.
- Objects had no encryption at rest, and metadata (mailbox address, message id) was in clear.
- The tenant's volumes were Longhorn with a `Delete` reclaim policy.
- Raw capture stayed OFF for this reason (NS-ADR-037).

## After

**Storage.** A static NFS PV `nervecenter-objects-nfs-pv` serves `192.168.42.8:/volume1/Kubernetes/NerveCenter/objects`:
- It uses NFS 4.1 and RWX, with reclaim policy **Retain**.
- The PVC `nervecenter-objects` is mounted in the api at `/data/objects`.
- The directory on the NAS is owned by uid/gid 10001 (the api user) with mode 0700.

**`backend/storage/filesystem.py` (`file://`).**
- Strict keys: segments of `[A-Za-z0-9._-]`, no `.`/`..`/empty segment, no leading `/`, at most 512 characters. A key can't escape the root.
- Atomic writes: a temp file, fsync, then rename. The payload is written before the meta file, so the meta file's presence means the object exists.
- Directories are 0700 and files 0600. (A test caught `mkdir(parents=True)` leaving parent directories at 0755; each directory is now created explicitly at 0700.)

**`backend/storage/encrypted.py`.**
- Each object is sealed with AES-256-GCM using a fresh nonce. The associated data is the format tag, the part name and the object key, so a ciphertext copied to another key fails.
- Caller metadata and the content type are encrypted inside a single envelope. The backend sees only `nse-v`, `nse-kid` and `nse-meta`.
- A keyring with rotation is configured through `NERVECENTER_OBJECT_STORE_ENCRYPTION_KEYS` (`kid:b64[,…]`): the first key encrypts and any listed key can decrypt.
- It fails closed: plaintext objects, unknown key ids and failed authentication all raise `ObjectDecryptionError`. There is no plaintext fallback.

**Factory.**
- `file://` always requires the keyring and is always encrypted.
- S3/MinIO is wrapped whenever a keyring is present.
- `object_store_status` reports `scheme=file, encrypted=True` and never echoes values.

**Key custody.** The key lives in a separate out-of-band Secret, `nervecenter-object-key`, with key id `k20261112` (32 bytes). It was generated on the operator machine with `secrets.token_bytes`, piped straight into `kubectl create`, and never printed or written to disk. The api gets it through `envFrom`.

**The owner must back this key up.** Without it, the stored mail can't be decrypted.

**Bootstrap Job.** The bootstrap Job now exits 0 for `file://` URLs, since there is no bucket to create.

## Cutover

| Step | Result |
|---|---|
| a. Deploy code + PV/PVC mount (still on MinIO, no key in the api) | volume mounted 10001/0700; no behavior change |
| b. Create `nervecenter-object-key` | key id `k20261112`, 32 bytes |
| c. Copy pass 1 (`backend.owner_tools.migrate_object_store`, one-off pod) | `{"copied": 631, "keys": 631}`; each copy read back and compared |
| d. Switch `NERVECENTER_OBJECT_STORE_URL` → `file:///data/objects`; api `envFrom` the key Secret; deploy | bootstrap skip path OK; api healthy |
| e. Copy pass 2 (gap) | `{"already_present": 631, "keys": 631}` |

The previous URL is kept in the Secret as `NERVECENTER_OBJECT_STORE_PREVIOUS_URL`, and the MinIO bucket and credentials stay in place, so rollback is a one-value change: point the URL back and remove the key `envFrom`. **The MinIO bucket was not modified or deleted.** It still holds the 631 plaintext objects. Deleting them from the shared tenant needs the owner's approval.

## Live verification (in the api pod, counts only)

- **Store:** `object_store_status` returns `configured / file / encrypted=True`, and the live factory is `EncryptedObjectStore`.
- **Existing objects:** all **631** communications have their object present. Each one decrypts to its recorded digest, and each `head().metadata["message_id"]` re-hashes to its key, which is the backfill identity binding.
- **Plaintext on the NAS:** **0** hits for a decrypted message id across every file on the NAS.
- **Write path:** a live put/get/head/delete probe round-trips, the on-disk file starts with `NSE1` and contains no plaintext, and the probe is removed afterwards.
- **Worker health:** 46 `mail_ingest.poll_ok` lines with 0 decryption, object-store, `CONTENT_UNAVAILABLE` or `OBJECT_MISSING` lines in the first 12 minutes. No new mail arrived in that window, so the first live ingest write is still to be observed.

## Tests

| Check | Result |
|---|---|
| `tests/backend/test_object_store_encrypted_fs.py` (new) | 27 passed |
| Existing storage tests | pass (70 total with the new file) |
| Full suite | 8477 passed, 1 failed (known date-dependent `test_routines`) |

The new tests cover:
- the filesystem round trip, and `created_at` kept on re-put;
- 8 traversal and malformed-key cases rejected;
- 0600 files and 0700 directories, with no temp files left behind;
- encrypted round trip with metadata, and a canary body, account, message id and content type all absent from disk;
- a renamed ciphertext failing, a tampered byte failing, key rotation, an unknown key and a wrong key with the same id;
- plaintext objects refused;
- 6 malformed keyrings;
- `file://` requiring keys, a relative path rejected, the status probe without values, and S3 wrapped only when keys are present;
- a fresh nonce on each put;
- the migration tool: verify-only, copy with read-back, idempotent re-run, source untouched, and no plaintext on disk.

## Consequences

- **Raw capture.** Raw capture (5.2) can now be stored encrypted on durable storage. It **stays OFF** until the owner turns it on.
- **Risk moved to key custody.** The new risk is that losing `nervecenter-object-key` loses the stored mail. Rotating means adding a new key first in the list and keeping the old one until objects are re-encrypted; there's no re-encryption tool yet.
