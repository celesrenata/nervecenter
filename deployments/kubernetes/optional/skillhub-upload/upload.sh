#!/bin/sh
# Copies the generated NerveCenter skill pack into /skills/nervecenter (NS-ADR-112).
# /skills/nervecenter is the ONLY writable mount (NFS subPath "nervecenter"); the
# pack comes from the read-only ConfigMap at /pack. Idempotent: each file is
# written to a temp name and renamed, so a re-run updates in place. Only slugs
# listed in this folder's own previous .pack-manifest are ever removed.
set -eu

DEST=/skills/nervecenter
PACK=/pack
cd "$DEST"

for f in "$PACK"/*.SKILL.md; do
  key=$(basename "$f")
  slug=${key%.SKILL.md}
  case "$slug" in
    ""|*[!a-z0-9-]*) echo "refusing slug '$slug'" >&2; exit 1 ;;
  esac
  mkdir -p "$slug"
  cp "$f" "$slug/.SKILL.md.tmp"
  mv -f "$slug/.SKILL.md.tmp" "$slug/SKILL.md"
done

# Remove only what an earlier run of this Job created and the pack no longer has.
if [ -f .pack-manifest ]; then
  grep -o '"[a-z0-9-]*": "[0-9a-f]\{64\}"' .pack-manifest | cut -d'"' -f2 | while read -r old; do
    if [ -n "$old" ] && [ ! -f "$PACK/$old.SKILL.md" ]; then
      rm -f "$old/SKILL.md"
      rmdir "$old" 2>/dev/null || true
    fi
  done
fi

cp "$PACK/pack-manifest.json" .pack-manifest.tmp
mv -f .pack-manifest.tmp .pack-manifest
echo "nervecenter skill pack written: $(ls -d */ | wc -l) folders"
