"""Which whisper.cpp builds exist, which a machine is offered, and zip names.

The pinned release as data, and the pure decisions about it, apart from the
module that downloads and installs (whisper_cpp_runtime) so they can be read —
and tested — without a disk or a network:

* **One release, pinned whole, by digest.** Tag b4938; every zip below was
  measured on the release page and through the GitHub API, which agreed. The
  URL names the tag, never "latest", so a new release cannot swap the bytes
  under a digest that no longer describes them.

* **Two builds are offered, of the six published.** The CPU build
  (`whisper-bin-x64.zip`, 8 MB) always: it runs everywhere and is the
  fallback of every other answer. The CUDA 12.4 build (671 MB) only to an
  NVIDIA card whose compute capability is known and lies in [5.0, 12.0) —
  `cuda_build_supported()`. The ceiling is the reason this rule exists: both
  CUDA builds of this release predate Blackwell, and on sm_120 a build with no
  kernels for it fails at run time after the user waited through 671 MB; the
  floor is CUDA 12's own, which dropped Kepler. An unknown capability gets no
  offer, for the same reason device.gpu_supports_int8() refuses one: offering a
  download that may not run costs far more than not offering it. Not offered:
  CUDA 11.8 (270 MB). What it reaches that 12.4 does not is a driver too old
  for CUDA 12 — from before the end of 2022, where updating the driver is the
  better fix than a second GPU build — and Kepler cards, whose last driver
  branch is itself out of support; for that, it would double the GPU matrix
  this module has to keep honest. The OpenBLAS build
  (21 MB) is a second CPU build whose gain over ggml's own kernels was never
  measured here, and a choice the user cannot judge is not a feature. Win32
  builds: WinZapp is 64-bit. No Vulkan build is published for Windows.

* **The zip's layout is not known, so it is not assumed.** Whether the files
  sit in `Release/` or at the root, and whether this release still ships an
  old `main.exe` beside `whisper-cli.exe`, was not verified: the extraction
  keeps the zip's own tree and `locate_executable()` finds whisper-cli.exe
  wherever it is. A zip without one is refused (WHISPER_CPP_CORRUPTED) rather
  than run under another name — `main.exe`, where recent releases still ship
  one, is a deprecation stub, not the program.

No user-facing text lives here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RELEASE_TAG = "b4938"
_RELEASE_URL = "https://github.com/ggml-org/whisper.cpp/releases/download/{tag}/{name}"

EXECUTABLE_NAME = "whisper-cli.exe"

# CUDA 12 has no Kepler (sm_3x) support; this release's CUDA builds have no
# Blackwell (sm_120) kernels. See the module docstring.
_CUDA_MIN_CAPABILITY = (5, 0)
_CUDA_UNSUPPORTED_FROM_CAPABILITY = (12, 0)

# Names Windows maps to a device whatever the folder or the extension: a file
# "nul.dll" written there goes nowhere, and "CON" opens the console.
_DOS_DEVICE = re.compile(
    r"^(?:con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(?:\..*)?$", re.I
)


@dataclass(frozen=True)
class RuntimeBuild:
    """One published Windows build of the pinned release."""

    id: str
    archive: str
    archive_bytes: int
    archive_sha256: str
    uses_cuda: bool

    @property
    def url(self) -> str:
        return _RELEASE_URL.format(tag=RELEASE_TAG, name=self.archive)


BUILD_CPU = RuntimeBuild(
    id="cpu",
    archive="whisper-bin-x64.zip",
    archive_bytes=8_361_840,
    archive_sha256="c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d",
    uses_cuda=False,
)
BUILD_CUDA = RuntimeBuild(
    id="cuda-12.4",
    archive="whisper-cublas-12.4.0-bin-x64.zip",
    archive_bytes=671_045_732,
    archive_sha256="c1b17166e1e31a91cc8e9c1f910d3785e3ce757bb2958bf9dce13fdb4880005f",
    uses_cuda=True,
)

BUILDS = (BUILD_CPU, BUILD_CUDA)


def cuda_build_supported(compute_capability) -> bool:
    """Whether the CUDA build may be offered to a card of this capability.

    None (NVML could not describe the card, or there is none) answers False.
    """
    if compute_capability is None:
        return False
    capability = tuple(compute_capability)
    return _CUDA_MIN_CAPABILITY <= capability < _CUDA_UNSUPPORTED_FROM_CAPABILITY


def builds_offered(compute_capability) -> tuple[RuntimeBuild, ...]:
    """The builds worth offering here: the CPU one, plus CUDA when it can run."""
    if cuda_build_supported(compute_capability):
        return (BUILD_CPU, BUILD_CUDA)
    return (BUILD_CPU,)


def safe_member_path(name):
    """A zip member's name as a relative path ("a/b.dll"), or None to refuse it.

    Refused: an empty name, an absolute one ("/x", "\\\\x"), any ".." segment,
    and any ":" — which covers a drive ("C:x") and an NTFS stream
    ("x.dll:evil") alike. Also refused, because Windows does not create what
    they say: a segment ending in "." or " " (silently trimmed, so "a.dll." and
    "a.dll" collide) and a DOS device name ("NUL", "com1.dll"). Both
    separators are read, since the zip is written elsewhere and extracted on
    Windows, where either one is a separator.
    """
    normalized = str(name or "").replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return None
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(_unsafe_segment(part) for part in parts):
        return None
    return "/".join(parts)


def _unsafe_segment(part) -> bool:
    return (
        part == ".."
        or ":" in part
        or part.endswith((".", " "))
        or _DOS_DEVICE.match(part) is not None
    )


def locate_executable(relative_paths):
    """The shallowest `whisper-cli.exe` among `relative_paths`, or None.

    Matched on the file name alone and without regard to case, because the
    folder it sits in is exactly what was not verified.
    """
    candidates = [
        path for path in relative_paths
        if path.rsplit("/", 1)[-1].lower() == EXECUTABLE_NAME
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda path: (path.count("/"), path))
