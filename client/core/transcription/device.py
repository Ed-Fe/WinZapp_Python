"""Where a transcription runs, and in which precision.

Probing and deciding are two different things and are kept apart on purpose.
Probing means loading the NVIDIA driver's own DLL and asking CTranslate2 how
many CUDA devices it can see — machine-specific, allowed to fail in a dozen
ways, and impossible to reproduce in a test. Deciding is a handful of
comparisons. So `probe_hardware()` is the only function here that touches the
machine, it returns its findings as a plain frozen dataclass, and every
decision function takes that dataclass as an argument. That is what lets the
"RTX 50xx picks the wrong precision" family of bugs be pinned by a test on a
machine with no GPU at all.

Nothing in this module raises for a hardware reason. A user who asked for CUDA
on a machine that has none gets the CPU and a reason code saying so — being
told is the point, failing is not, and for a blind user an error dialog in
place of a transcription is strictly worse than a slower transcription.

**Probe immediately before deciding, and never reuse a probe.**
available_memory_mb() plans against memory that is *free*, so when the probe
ran is part of the answer. A probe taken at startup is taken before Chromium
has loaded WhatsApp Web, before the media cache has filled and possibly before
the screen reader is even up: it reports the high number this module already
says is misleading, and auto_select_model() would then pick a model against
memory that no longer exists by the time it loads. probe_hardware() costs one
DLL load and three driver calls, which is nothing next to a transcription, so
call it at the point of decision every time.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from dataclasses import dataclass

from core.transcription import model_catalog

DEVICE_CPU = "cpu"
DEVICE_CUDA = "cuda"

# What the user picked in the settings. "auto" is the default and the only one
# that is allowed to change its mind between launches.
PREFERENCE_AUTO = "auto"
PREFERENCE_CUDA = "cuda"
PREFERENCE_CPU = "cpu"

COMPUTE_INT8 = "int8"
COMPUTE_FLOAT16 = "float16"
COMPUTE_FLOAT32 = "float32"

# Why the device below was chosen. Symbolic, never a sentence: the UI maps them
# through DEVICE_REASON_I18N_KEYS so the announcement is in the user's language.
REASON_CUDA_SELECTED = "cuda_selected"
REASON_CPU_REQUESTED = "cpu_requested"
# The user asked for CUDA and there is none — this one has to be announced.
REASON_CUDA_UNAVAILABLE = "cuda_unavailable"
# Same situation under "auto", where the user asked for nothing in particular
# and does not need to hear it as a thwarted request.
REASON_NO_CUDA_FOUND = "no_cuda_found"
# CUDA devices were counted and then could not be questioned — a driver fault
# on a machine that really does have a card, which the user can act on.
# probe_hardware() never produces this reason on its own, and re-probing cannot
# either: a fresh probe would simply count the device again and land on
# CUDA_SELECTED. It takes a caller that demotes the card deliberately after a
# run has failed on it — dataclasses.replace(probe, cuda_available=False,
# cuda_device_count=0, cuda_probe_error=<what the run reported>) — which is the
# only state where "your driver is broken" is the right thing to say.
REASON_CUDA_DRIVER_ERROR = "cuda_driver_error"

DEVICE_REASON_I18N_KEYS = {
    REASON_CUDA_SELECTED: "transcription_device_cuda_selected",
    REASON_CPU_REQUESTED: "transcription_device_cpu_requested",
    REASON_CUDA_UNAVAILABLE: "transcription_device_cuda_unavailable",
    REASON_NO_CUDA_FOUND: "transcription_device_no_cuda_found",
    REASON_CUDA_DRIVER_ERROR: "transcription_device_cuda_driver_error",
}

# float16 needs Volta or newer to be worth anything; on Pascal and older the
# half-precision path is emulated and slower than float32, when it works.
_FLOAT16_MIN_CAPABILITY = (7, 0)

# Blackwell (sm_120) and anything after it.
_INT8_UNSUPPORTED_FROM_CAPABILITY = (12, 0)

# How much room auto_select_model() insists on beyond a model's recommended
# minimum. The minimums in model_catalog already cover weights, activations and
# the CUDA context; the extra 25% is for what else is on the machine while
# WinZapp transcribes — Chromium running WhatsApp Web (which also takes VRAM
# for compositing), the screen reader, and whatever the user was doing. Getting
# this wrong is not a graceful degradation: CTranslate2 aborts the run with an
# allocation error, after the user already waited for the model to load.
_MEMORY_HEADROOM = 1.25

# Where nvml.dll lives. The bare name first, then the two absolute paths pynvml
# itself falls back to: the DLL is installed by the display driver, and on a
# machine whose search order does not reach System32 (or that still has the
# legacy NVSMI layout) the bare name fails while the card is perfectly usable.
# That miss is not free — it drops the machine into the "VRAM unknown" branch,
# which is exactly where auto_select_model() has the least to work with.
_NVML_LIBRARY_PATHS = (
    "nvml.dll",
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "nvml.dll"),
    os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        "NVIDIA Corporation", "NVSMI", "nvml.dll",
    ),
)


@dataclass(frozen=True)
class HardwareProbe:
    """What `probe_hardware()` managed to find out. Every field may be absent.

    A field being None means "unknown", never "zero": a decision that treats an
    unknown VRAM figure as 0 would silently hide every GPU whose driver DLL
    failed to load.

    The two error fields are not interchangeable, and conflating them is what
    made a machine with no NVIDIA hardware at all report a graphics driver
    fault. `driver_error` is an **aggregate** of everything that failed during
    the probe — a missing ctranslate2, NVML, the Windows memory API — and is
    log-facing only, like TranscriptionError's detail. `cuda_probe_error` is the
    narrow one: a GPU was counted and then could not be described. Only that one
    may drive a spoken reason, because the aggregate is non-empty on every
    machine that has not installed the transcription backend yet — which is all
    of them until the user asks for it.
    """

    cuda_available: bool = False
    cuda_device_count: int = 0
    compute_capability: tuple[int, int] | None = None
    total_vram_mb: int | None = None
    free_vram_mb: int | None = None
    total_ram_mb: int | None = None
    available_ram_mb: int | None = None
    cuda_probe_error: str | None = None
    driver_error: str | None = None


def resolve_device(preference, probe) -> tuple[str, str]:
    """(device, reason) for a stated preference and a probe result.

    An unknown preference is treated as "auto" rather than rejected — it is a
    settings value, and a settings file written by an older or newer WinZapp
    must not be able to make transcription unavailable.
    """
    if preference == PREFERENCE_CPU:
        return DEVICE_CPU, REASON_CPU_REQUESTED

    if cuda_usable(probe):
        return DEVICE_CUDA, REASON_CUDA_SELECTED

    if preference == PREFERENCE_CUDA:
        # Asked for explicitly, so the fallback is worth saying out loud, and a
        # card that is present but unreachable is worth distinguishing from "no
        # GPU here": the first is usually a driver the user can reinstall.
        # Keyed on cuda_probe_error, never on the driver_error aggregate — that
        # one carries "ctranslate2 is not installed", i.e. every user's state
        # until the backend ships, and sending those people to reinstall a
        # graphics driver they may not even have is worse than saying nothing.
        if probe.cuda_probe_error:
            return DEVICE_CPU, REASON_CUDA_DRIVER_ERROR
        return DEVICE_CPU, REASON_CUDA_UNAVAILABLE

    return DEVICE_CPU, REASON_NO_CUDA_FOUND


def cuda_usable(probe) -> bool:
    """Whether CUDA can actually be used, not merely whether a DLL loaded."""
    return bool(probe.cuda_available) and probe.cuda_device_count > 0


def gpu_supports_int8(compute_capability) -> bool:
    """Whether int8 kernels exist for this GPU at all.

    This is the reason this module exists. The CTranslate2 builds that support
    sm_120 (>= 4.6.3, CUDA 12.8) are compiled with INT8 *disabled* for it, so on
    a Blackwell card every int8 variant — "int8", "int8_float16",
    "int8_bfloat16" — fails at model load with an unsupported-compute-type
    error, and the user hears nothing but "internal error".

    An unknown capability answers False: refusing int8 costs speed, offering it
    to a card that cannot run it costs the transcription.
    """
    if compute_capability is None:
        return False
    return tuple(compute_capability) < _INT8_UNSUPPORTED_FROM_CAPABILITY


def _int8_safe(compute, compute_capability) -> str:
    """`compute`, unless it is an int8 flavour this GPU has no kernels for.

    The sm_120 rule on the execution path rather than in a comment.
    select_compute_type() asks for float16 today, so this changes nothing
    today — it is what keeps the rule true the day a card short of VRAM makes
    int8_float16 look attractive, since that failure surfaces only at model
    load, on hardware no test runner has, after the user has already waited
    through a multi-gigabyte download.
    """
    if str(compute).startswith("int8") and not gpu_supports_int8(compute_capability):
        return COMPUTE_FLOAT16
    return compute


def select_compute_type(device, probe) -> str:
    """The CTranslate2 compute type to load the model with."""
    if device != DEVICE_CUDA:
        # int8 on the CPU is the whole reason CPU transcription is usable at
        # all — roughly three times faster than float32, with no audible
        # difference on speech.
        return COMPUTE_INT8

    capability = probe.compute_capability
    if capability is not None and tuple(capability) < _FLOAT16_MIN_CAPABILITY:
        return COMPUTE_FLOAT32

    # float16 everywhere else, including sm_120. An unknown capability lands
    # here too — a card CTranslate2 can see but NVML could not describe is far
    # more likely to be new than pre-Volta.
    return _int8_safe(COMPUTE_FLOAT16, capability)


def auto_select_model(probe, device, installed_ids, catalog=None):
    """The model id to use when the user has not chosen one, or None.

    Two rules, in this order:

    1. If a model that is already on disk fits, use the largest of those. A
       silent 3 GB download nobody asked for is worse than transcribing with
       what is already there — and on a metered connection it is a lot worse.
    2. Otherwise the largest model that fits, which the caller will have to
       download.

    "Fits" means the device's memory covers the model's recommended minimum
    with `_MEMORY_HEADROOM` to spare. When the memory could not be measured at
    all, only rule 1 applies and only for the smallest installed model: guessing
    upwards on an unknown machine is how a run dies half way through.
    """
    models = tuple(catalog) if catalog is not None else model_catalog.list_models()
    # Sorted by what each model costs *on this device* rather than trusting the
    # catalogue's own order, so "the largest that fits" still means the most
    # demanding one when a caller passes its own list (the tests do).
    models = sorted(models, key=lambda m: (_requirement_mb(m, device), m.download_bytes))
    installed = set(installed_ids or ())
    budget_mb = available_memory_mb(probe, device)

    if budget_mb is None:
        installed_models = [m for m in models if m.id in installed]
        return installed_models[0].id if installed_models else None

    fitting = [m for m in models if model_fits(m, budget_mb, device)]
    already_here = [m for m in fitting if m.id in installed]
    if already_here:
        return already_here[-1].id
    return fitting[-1].id if fitting else None


def available_memory_mb(probe, device):
    """Memory the transcription may plan around, in MB, or None if unknown.

    Free memory wins over total on both devices, and for the same reason: the
    machine is not idle while WinZapp transcribes. Chromium is running WhatsApp
    Web (holding RAM, and VRAM for compositing), a screen reader is running, and
    the user has their own work open. A 16 GB machine with 11 GB in use is a
    5 GB machine for this purpose, and planning against the 16 buys a 3 GB
    download followed by an allocation error.
    """
    if device == DEVICE_CUDA:
        free, total = probe.free_vram_mb, probe.total_vram_mb
    else:
        free, total = probe.available_ram_mb, probe.total_ram_mb
    return free if free is not None else total


def model_fits(model, budget_mb, device) -> bool:
    """Whether `model` fits in `budget_mb` on `device`, headroom included."""
    if budget_mb is None:
        return False
    return budget_mb >= _requirement_mb(model, device) * _MEMORY_HEADROOM


def _requirement_mb(model, device) -> int:
    """The model's recommended minimum for the device it will run on."""
    return model.min_vram_mb if device == DEVICE_CUDA else model.min_ram_mb


def device_reason_i18n_key(reason) -> str:
    """The i18n key announcing a device reason.

    Unknown reasons resolve to the plain CPU message rather than to a key of
    their own, for the same reason as errors.error_i18n_key(): I18n.t() would
    otherwise have the screen reader read the code itself.
    """
    return DEVICE_REASON_I18N_KEYS.get(
        reason, DEVICE_REASON_I18N_KEYS[REASON_CPU_REQUESTED]
    )


def probe_hardware() -> HardwareProbe:
    """Ask the machine what it has. Never raises; unknown fields stay None.

    Three independent questions, each allowed to fail on its own:

    * how many CUDA devices CTranslate2 can drive — ctranslate2 is an optional
      dependency, so the import lives here rather than at module level;
    * VRAM and compute capability, read from the driver's own ``nvml.dll``
      through ctypes. nvidia-ml-py is deliberately not a dependency: it would
      ship to every user for one call that only matters on the machines that
      have the DLL anyway;
    * RAM, total and available, from GlobalMemoryStatusEx.

    Only NVML's failure becomes `cuda_probe_error`, and only once a device has
    actually been counted — see HardwareProbe for why the aggregate must never
    be read as a driver fault.
    """
    cuda_count = 0
    capability = None
    total_vram = None
    free_vram = None
    cuda_probe_error = None
    # One bag for everything that failed, joined into driver_error at the end:
    # it is log-facing only, so it is more useful complete than tidy.
    problems = []

    try:
        # Imported here, never at module level: ctranslate2 may not be
        # installed at all, and the menu that offers to install it has to work
        # on exactly those machines.
        import ctranslate2

        cuda_count = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:
        problems.append(f"ctranslate2: {exc}")

    # Both helpers already answer with an error string instead of raising, and
    # both are still called inside a guard: "never raises" is this function's
    # promise to the UI, not something it may inherit from a helper that loads
    # a third-party driver DLL and calls into it through ctypes.
    if cuda_count > 0:
        try:
            capability, total_vram, free_vram, cuda_probe_error = _probe_nvml()
        except Exception as exc:
            cuda_probe_error = f"nvml: {exc}"
        if cuda_probe_error:
            problems.append(cuda_probe_error)

    try:
        total_ram, available_ram, ram_error = _probe_ram_mb()
    except Exception as exc:
        total_ram = available_ram = None
        ram_error = f"ram: {exc}"
    if ram_error:
        problems.append(ram_error)

    probe = HardwareProbe(
        cuda_available=cuda_count > 0,
        cuda_device_count=cuda_count,
        compute_capability=capability,
        total_vram_mb=total_vram,
        free_vram_mb=free_vram,
        total_ram_mb=total_ram,
        available_ram_mb=available_ram,
        cuda_probe_error=cuda_probe_error,
        driver_error="; ".join(problems) or None,
    )
    logging.info("[transcription] hardware probe: %s", probe)
    return probe


def _load_nvml():
    """(library, error) — the first nvml.dll that loads, or None and why not.

    Every failure is kept, not just the last one: reporting only the last leaves
    the log blaming the legacy NVSMI path on every machine without an NVIDIA
    card, which is the least informative of the three and says nothing about
    why the normal locations failed.

    CDLL rather than WinDLL, matching pynvml, whose fallback paths these are:
    NVML is a cdecl library. On x64 the two calling conventions are the same, so
    this is about agreeing with the source of the convention, not about a bug.
    """
    failures = []
    for path in _NVML_LIBRARY_PATHS:
        try:
            return ctypes.CDLL(path), None
        except Exception as exc:
            failures.append(f"{path}: {exc}")
    return None, "; ".join(failures)


def _probe_nvml():
    """(capability, total_vram_mb, free_vram_mb, error) from nvml.dll.

    Device 0 only. On a single-GPU machine — which is what this is for — there
    is nothing to choose. On a multi-GPU one the two device 0s need not be the
    same card: NVML orders by PCI bus id while CUDA orders by FASTEST_FIRST
    unless CUDA_DEVICE_ORDER=PCI_BUS_ID is set. Guessing which pairing applies
    would be worse than reading the first card, since the figures only feed a
    size decision that already carries 25% of headroom.
    """
    if not sys.platform.startswith("win"):
        return None, None, None, "nvml: not Windows"

    class _NvmlMemory(ctypes.Structure):
        _fields_ = [
            ("total", ctypes.c_ulonglong),
            ("free", ctypes.c_ulonglong),
            ("used", ctypes.c_ulonglong),
        ]

    nvml, load_error = _load_nvml()
    if nvml is None:
        return None, None, None, f"nvml: {load_error}"

    initialised = False
    try:
        if nvml.nvmlInit_v2() != 0:
            return None, None, None, "nvml: nvmlInit_v2 failed"
        initialised = True

        handle = ctypes.c_void_p()
        if nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
            return None, None, None, "nvml: no handle for device 0"

        capability = None
        major = ctypes.c_int()
        minor = ctypes.c_int()
        if nvml.nvmlDeviceGetCudaComputeCapability(
            handle, ctypes.byref(major), ctypes.byref(minor)
        ) == 0:
            capability = (major.value, minor.value)

        total_mb = free_mb = None
        memory = _NvmlMemory()
        if nvml.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) == 0:
            total_mb = int(memory.total // (1024 * 1024))
            free_mb = int(memory.free // (1024 * 1024))

        return capability, total_mb, free_mb, None
    except Exception as exc:
        # A driver too old for one of these entry points, or one that faults
        # inside them: either way the answer is "unknown", never a crash.
        return None, None, None, f"nvml: {exc}"
    finally:
        # Only after a successful init: nvmlShutdown() against a library that
        # never initialised is not a documented no-op, and this runs on the
        # path where the driver is already misbehaving.
        if initialised:
            try:
                nvml.nvmlShutdown()
            except Exception:
                pass


def _probe_ram_mb():
    """(total_ram_mb, available_ram_mb, error) from GlobalMemoryStatusEx.

    Available as well as total, because available is what the CPU path plans
    against — see available_memory_mb().
    """

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        if sys.platform.startswith("win"):
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None, None, "ram: GlobalMemoryStatusEx failed"
            return (
                int(status.ullTotalPhys // (1024 * 1024)),
                int(status.ullAvailPhys // (1024 * 1024)),
                None,
            )

        page_size = os.sysconf("SC_PAGE_SIZE")
        total = page_size * os.sysconf("SC_PHYS_PAGES")
        available = page_size * os.sysconf("SC_AVPHYS_PAGES")
        return int(total // (1024 * 1024)), int(available // (1024 * 1024)), None
    except Exception as exc:
        return None, None, f"ram: {exc}"
