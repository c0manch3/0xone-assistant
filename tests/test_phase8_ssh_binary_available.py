"""Phase 8 hotfix — assert the OpenSSH client is on PATH.

``vault_sync.git_ops`` invokes ``git push`` with
``GIT_SSH_COMMAND="ssh -i <deploy_key> -o IdentitiesOnly=yes -o
StrictHostKeyChecking=yes -o UserKnownHostsFile=<known_hosts>"`` so
``git`` shells out to ``ssh`` for the transport leg. Without the
``ssh`` binary on PATH the push fails immediately with
``ssh: not found`` and the daemon emits the failure as
``vault sync failed: push: ssh ...: 1: ssh: not found``.

Phase 8's pytest suite mocks ``git_push`` and never invokes the real
subprocess, so this dependency gap survived 4 reviewer waves and only
surfaced on the first live tick after the Docker deploy.

Running this test inside the CI test container (which extends the
runtime stage) fails loudly the moment ``openssh-client`` is dropped
from the runtime image again. On dev hosts (Mac/Linux) ``ssh`` is
universally present, so the assertion is effectively a no-op there
and a regression guard in CI.
"""

from __future__ import annotations

import shutil


def test_ssh_binary_available_for_vault_sync() -> None:
    """``ssh`` must resolve via PATH for vault_sync ``git push`` to work."""
    assert shutil.which("ssh") is not None, (
        "ssh binary missing — add openssh-client to Dockerfile runtime stage "
        "(phase 8 vault_sync's GIT_SSH_COMMAND requires the OpenSSH client)"
    )
