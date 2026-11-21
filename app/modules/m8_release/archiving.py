from app.archive import lookup_release, release_for_run, review, save_release
from app.config import VERSION
from app.manifest import hash_payload, timestamp
from app.modules.m8_release.service import release_document
from app.reasons import FabricError


def release_identity(run_id, cancel_id, substitute):
    payload = {"run": run_id, "cancel": cancel_id, "substitute": substitute, "rules": VERSION}
    return hash_payload(payload)


def submit_release(archive, run_id, cancel_id, substitute):
    stored = archive.read(run_id)
    release_id = release_identity(run_id, cancel_id, substitute)
    with archive.write() as db:
        current = lookup_release(db, release_id)
        if current:
            return current["manifest"], True
        if release_for_run(db, run_id) is not None:
            raise FabricError("RELEASE_EXISTS", 409)
        document = release_document(run_id, stored["snapshot"], cancel_id, substitute, release_id, timestamp())
        save_release(db, document)
        review(db, run_id, document["manifest"]["created_at"], "released")
        return document["manifest"], False
