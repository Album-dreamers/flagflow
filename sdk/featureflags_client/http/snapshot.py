import contextlib
import json
import logging
import os
import tempfile
from typing import Optional

from featureflags_client.http.types import PreloadFlagsResponse

log = logging.getLogger(__name__)


class Snapshot:
    """
    Last state received from the server, kept on disk so that an app
    starting while the server is unreachable still gets the real rules
    instead of the in-code defaults.

    Best effort: failing to write it never breaks syncing.
    """

    def __init__(self, path: str, project: str) -> None:
        self.path = os.path.abspath(path)
        self.project = project

    def save(self, response: PreloadFlagsResponse) -> None:
        data = {"project": self.project, **response.to_dict()}
        # The temp file must be on the same filesystem as the target for
        # os.replace to be atomic, so it goes into the same directory.
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(self.path), prefix=".featureflags-"
            )
        except OSError as exc:
            log.warning("Failed to save flags snapshot: %r", exc)
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, self.path)
        except Exception as exc:
            log.warning("Failed to save flags snapshot: %r", exc)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    def load(self) -> Optional[PreloadFlagsResponse]:
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
            if data.pop("project", None) != self.project:
                log.warning(
                    "Flags snapshot %s is not for project %s, ignoring it",
                    self.path,
                    self.project,
                )
                return None
            return PreloadFlagsResponse.from_dict(data)
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning("Failed to load flags snapshot: %r", exc)
            return None
