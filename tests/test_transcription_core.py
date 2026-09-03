"""The pure core of local (Whisper) transcription: catalogue, device, errors.

Everything here is a decision that is impossible to observe on the machine that
made it, which is why the decisions were written as functions over data in the
first place:

* **The catalogue is a contract with Hugging Face.** The file list is not the
  same for every repository — the Systran tiny..medium conversions ship
  ``vocabulary.txt`` and no ``preprocessor_config.json``, large-v3 and the turbo
  conversion ship ``vocabulary.json`` *and* ``preprocessor_config.json``. A
  download built on one assumed shape leaves a model that looks complete and
  that CTranslate2 refuses to load, which the user experiences as "it just says
  internal error".

* **sm_120 must never be given int8.** The CTranslate2 builds that support
  Blackwell (>= 4.6.3, CUDA 12.8) are compiled with INT8 disabled for it, so
  every int8 variant fails at model load on the reporter's own card. That is a
  property of a machine no test runner has, so it is pinned as a property of
  ``select_compute_type()`` instead.

* **Asking for CUDA on a machine without it is not an error.** It falls back to
  the CPU with a reason code the UI announces. A raised exception here would
  replace a slower transcription with nothing at all. And the reason has to come
  from ``cuda_probe_error``, never from the ``driver_error`` aggregate: the
  aggregate is non-empty on every machine that has not installed the backend
  yet, so reading it as a driver fault told users with no NVIDIA hardware at all
  to go and reinstall a graphics driver.

* **Memory is planned against what is free, not what is installed.** A 16 GB
  machine running Chromium, WhatsApp Web and a screen reader is a 5 GB machine
  for this purpose; deciding on the 16 buys a 3 GB download and then an
  allocation error mid-transcription. Pinned on both devices, symmetrically.

* **A code with no translation is spoken as the code.** ``I18n.t()`` is
  ``translations.get(key, key)``, so a failure mode nobody translated has NVDA
  read "transcription_error_ffmpeg_failed" out loud. The last two tests walk
  every code there is, in every locale.
"""

import ast
import dataclasses
import json
import os
import sys
import types

import pytest

from app_paths import resource_path
from core.transcription import device, errors, model_catalog


def _load(name):
    with open(resource_path("languages", f"{name}.json"), "r", encoding="utf-8") as f:
        return json.load(f)


LOCALES = sorted(_load("language_map"))

# Repository, pinned revision, model.bin digest and the exact size of every
# file, as read from the Hugging Face API on 2026-09-03 — restated here rather
# than imported, so the catalogue is checked against the observation instead of
# against itself. Every size is a constant because the revision is pinned, and
# that is what lets "the wrong size" be a detectable state for all five files
# rather than only for the weights.
_EXPECTED_REPOS = {
    "tiny": (
        "Systran/faster-whisper-tiny",
        "d90ca5fe260221311c53c58e660288d3deb8d356",
        "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1",
        {"config.json": 2_249, "model.bin": 75_538_270,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "base": (
        "Systran/faster-whisper-base",
        "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9",
        {"config.json": 2_309, "model.bin": 145_217_532,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "small": (
        "Systran/faster-whisper-small",
        "536b0662742c02347bc0e980a01041f333bce120",
        "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
        {"config.json": 2_370, "model.bin": 483_546_902,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "medium": (
        "Systran/faster-whisper-medium",
        "08e178d48790749d25932bbc082711ddcfdfbc4f",
        "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
        {"config.json": 2_257, "model.bin": 1_527_906_378,
         "tokenizer.json": 2_203_239, "vocabulary.txt": 459_861},
    ),
    "large-v3": (
        "Systran/faster-whisper-large-v3",
        "edaa852ec7e145841d8ffdb056a99866b5f0a478",
        "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
        {"config.json": 2_394, "model.bin": 3_087_284_237,
         "preprocessor_config.json": 340, "tokenizer.json": 2_480_617,
         "vocabulary.json": 1_068_114},
    ),
    "large-v3-turbo": (
        "deepdml/faster-whisper-large-v3-turbo-ct2",
        "4df90f75321148c3a29a9e2351b7ddf8f5b115a8",
        "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
        {"config.json": 2_263, "model.bin": 1_617_884_929,
         "preprocessor_config.json": 340, "tokenizer.json": 2_710_337,
         "vocabulary.json": 1_068_114},
    ),
}


def _probe(**kwargs):
    """A HardwareProbe with everything unknown unless the test says otherwise."""
    return device.HardwareProbe(**kwargs)


def _cuda_probe(capability=(8, 6), free_vram_mb=8192, **kwargs):
    kwargs.setdefault("total_vram_mb", free_vram_mb)
    return device.HardwareProbe(
        cuda_available=True,
        cuda_device_count=1,
        compute_capability=capability,
        free_vram_mb=free_vram_mb,
        **kwargs,
    )


class TestModelCatalog:
    def test_ids_are_unique(self):
        ids = [m.id for m in model_catalog.MODELS]
        assert sorted(ids) == sorted(set(ids))

    def test_catalogue_is_exactly_the_verified_set(self):
        assert {m.id for m in model_catalog.MODELS} == set(_EXPECTED_REPOS)

    @pytest.mark.parametrize("model_id", sorted(_EXPECTED_REPOS))
    def test_repo_and_files_match_hugging_face(self, model_id):
        repo, _revision, _digest, files = _EXPECTED_REPOS[model_id]
        model = model_catalog.get_model(model_id)
        assert model.repo == repo
        assert dict(model.files) == files
        assert model.model_bin_bytes == files["model.bin"]

    @pytest.mark.parametrize("model_id", sorted(_EXPECTED_REPOS))
    def test_the_totals_are_the_exact_sum_of_the_files(self, model_id):
        # The totals used to add a guessed couple of megabytes for the
        # auxiliary files, and on the turbo conversion that guess was 181 KB
        # UNDER the real figure — the wrong direction for a number that feeds a
        # "does this fit?" gate and a free-space check.
        _repo, _revision, _digest, files = _EXPECTED_REPOS[model_id]
        model = model_catalog.get_model(model_id)
        assert model.download_bytes == sum(files.values())
        assert model.disk_bytes == model.download_bytes

    @pytest.mark.parametrize("model_id", sorted(_EXPECTED_REPOS))
    def test_every_model_carries_the_verified_weights_digest(self, model_id):
        # Pinned revision plus digest is what makes MODEL_CORRUPTED provable
        # for the one file where a silent corruption costs a multi-gigabyte
        # re-download to discover.
        _repo, _revision, digest, _files = _EXPECTED_REPOS[model_id]
        model = model_catalog.get_model(model_id)
        assert model.model_bin_sha256 == digest
        assert len(model.model_bin_sha256) == 64
        assert set(model.model_bin_sha256) <= set("0123456789abcdef")

    @pytest.mark.parametrize("model_id", sorted(_EXPECTED_REPOS))
    def test_every_model_is_pinned_to_the_verified_revision(self, model_id):
        # Downloads resolve by sha, not by /main/: a revision published upstream
        # must not silently replace weights an install already verified, nor
        # invalidate the byte counts above.
        _repo, revision, _digest, _files = _EXPECTED_REPOS[model_id]
        model = model_catalog.get_model(model_id)
        assert model.revision == revision
        assert len(model.revision) == 40
        assert set(model.revision) <= set("0123456789abcdef")

    def test_every_file_can_be_checked_by_size_not_just_the_weights(self):
        # errors.MODEL_CORRUPTED promises "missing or the wrong size", which is
        # only true if every file has an exact expected size: a truncated
        # tokenizer.json is as plausible-looking as a truncated model.bin, and
        # fails the load just the same.
        for model in model_catalog.MODELS:
            sizes = dict(model.files)
            assert len(sizes) == len(model.files), "duplicate file name"
            for name, size in sizes.items():
                assert isinstance(size, int) and size > 0, name

    def test_a_truncated_file_changes_the_expected_total(self):
        medium = dict(model_catalog.get_model("medium").files)
        for damaged_file in ("model.bin", "tokenizer.json", "config.json"):
            damaged = dict(medium)
            damaged[damaged_file] -= 1
            assert sum(damaged.values()) != sum(medium.values())

    def test_the_two_file_layouts_are_not_confused(self):
        # The regression this guards: one shared file list for all six repos.
        assert "vocabulary.txt" in dict(model_catalog.get_model("small").files)
        assert "vocabulary.json" in dict(model_catalog.get_model("large-v3").files)
        assert "preprocessor_config.json" not in dict(
            model_catalog.get_model("medium").files
        )
        assert "preprocessor_config.json" in dict(
            model_catalog.get_model("large-v3-turbo").files
        )

    def test_every_model_ships_the_weights_and_a_tokenizer(self):
        for model in model_catalog.MODELS:
            names = dict(model.files)
            assert "model.bin" in names
            assert "tokenizer.json" in names
            assert "config.json" in names

    def test_the_file_table_cannot_be_edited_in_place(self):
        # A dict here would let one caller rewrite the expected sizes for every
        # other caller in the process; a tuple of pairs cannot be.
        assert isinstance(model_catalog.MODELS[0].files, tuple)
        for entry in model_catalog.MODELS[0].files:
            assert isinstance(entry, tuple) and len(entry) == 2

    def test_list_models_is_ordered_by_size_class_then_bytes(self):
        listed = model_catalog.list_models()
        assert len(listed) == len(model_catalog.MODELS)
        keys = [
            (model_catalog.SIZE_CLASSES.index(m.size_class), m.download_bytes)
            for m in listed
        ]
        assert keys == sorted(keys)

    def test_the_cheapest_model_comes_first_and_the_costliest_last(self):
        listed = model_catalog.list_models()
        assert listed[0].id == "tiny"
        assert listed[-1].id == "large-v3"

    def test_memory_requirements_never_decrease_with_size(self):
        listed = model_catalog.list_models()
        assert [m.min_vram_mb for m in listed] == sorted(m.min_vram_mb for m in listed)
        assert [m.min_ram_mb for m in listed] == sorted(m.min_ram_mb for m in listed)

    def test_every_size_class_is_one_of_the_declared_ones(self):
        for model in model_catalog.MODELS:
            assert model.size_class in model_catalog.SIZE_CLASSES

    def test_unknown_id_returns_none_rather_than_raising(self):
        # It comes from settings, where a model removed between versions is a
        # normal state and must not crash the conversation window.
        assert model_catalog.get_model("large-v4") is None
        assert model_catalog.get_model(None) is None
        assert model_catalog.get_model("") is None

    def test_total_bytes_matches_the_entry(self):
        model = model_catalog.get_model("base")
        assert model_catalog.total_bytes("base") == model.download_bytes

    def test_total_bytes_of_an_unknown_id_is_none_not_zero(self):
        # 0 would be a trap: a settings file naming a model some later version
        # dropped would tell the free-space gate "it fits" and start a download
        # of something that does not exist.
        assert model_catalog.total_bytes("large-v4") is None
        assert model_catalog.total_bytes(None) is None

    def test_models_are_frozen(self):
        # The catalogue is shared module state; a caller adjusting one entry
        # would change it for every other caller in the process.
        with pytest.raises(dataclasses.FrozenInstanceError):
            model_catalog.MODELS[0].disk_bytes = 1


class TestResolveDevice:
    def test_cpu_preference_is_honoured_even_with_a_gpu_present(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_CPU, _cuda_probe())
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CPU_REQUESTED

    def test_auto_takes_the_gpu_when_there_is_one(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_AUTO, _cuda_probe())
        assert chosen == device.DEVICE_CUDA
        assert reason == device.REASON_CUDA_SELECTED

    def test_auto_falls_back_to_the_cpu_quietly(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_AUTO, _probe())
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_NO_CUDA_FOUND

    def test_cuda_preference_takes_the_gpu(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_CUDA, _cuda_probe())
        assert chosen == device.DEVICE_CUDA
        assert reason == device.REASON_CUDA_SELECTED

    def test_cuda_without_cuda_is_a_fallback_not_an_exception(self):
        chosen, reason = device.resolve_device(device.PREFERENCE_CUDA, _probe())
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CUDA_UNAVAILABLE

    def test_cuda_with_a_driver_failure_says_so_specifically(self):
        # This state does not come out of probe_hardware(), and re-probing
        # cannot produce it either — a fresh probe would count the device again
        # and land on CUDA_SELECTED. It takes a caller that demotes the card
        # deliberately after a run failed on it, i.e. dataclasses.replace(
        # probe, cuda_available=False, cuda_device_count=0, cuda_probe_error=…),
        # which is what is built by hand here.
        probe = _probe(cuda_probe_error="nvml: could not load nvml.dll",
                       driver_error="nvml: could not load nvml.dll")
        chosen, reason = device.resolve_device(device.PREFERENCE_CUDA, probe)
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CUDA_DRIVER_ERROR

    def test_an_unrelated_probe_failure_is_not_a_driver_fault(self):
        # driver_error is an aggregate of everything the probe could not do —
        # the Windows memory API here. Reading it as "the graphics driver
        # failed" sends the user off to reinstall a driver they may not have.
        probe = _probe(driver_error="ram: GlobalMemoryStatusEx failed")
        chosen, reason = device.resolve_device(device.PREFERENCE_CUDA, probe)
        assert chosen == device.DEVICE_CPU
        assert reason == device.REASON_CUDA_UNAVAILABLE

    def test_a_missing_backend_is_not_a_driver_fault(self):
        # The state of EVERY install until the backend is installed: the
        # aggregate carries the failed ctranslate2 import and nothing else.
        probe = _probe(driver_error="ctranslate2: No module named ctranslate2")
        _, reason = device.resolve_device(device.PREFERENCE_CUDA, probe)
        assert reason == device.REASON_CUDA_UNAVAILABLE
        _, auto_reason = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert auto_reason == device.REASON_NO_CUDA_FOUND

    def test_a_driver_error_alongside_a_working_gpu_does_not_block_it(self):
        # NVML failing only costs us the VRAM figure; CTranslate2 can still run.
        probe = _cuda_probe(capability=None, free_vram_mb=None, total_vram_mb=None,
                            driver_error="nvml: nvmlInit_v2 failed")
        chosen, _ = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert chosen == device.DEVICE_CUDA

    def test_a_zero_device_count_is_not_cuda(self):
        # ctranslate2 loaded but found nothing — "available" alone is not enough.
        probe = device.HardwareProbe(cuda_available=True, cuda_device_count=0)
        chosen, _ = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert chosen == device.DEVICE_CPU

    @pytest.mark.parametrize("preference", [None, "", "gpu", "gpu ", 3])
    def test_an_unrecognised_preference_behaves_like_auto(self, preference):
        # It is a settings value; a file from another version must not be able
        # to make transcription unavailable.
        assert device.resolve_device(preference, _cuda_probe())[0] == device.DEVICE_CUDA
        assert device.resolve_device(preference, _probe())[0] == device.DEVICE_CPU

    @pytest.mark.parametrize("reason", sorted(device.DEVICE_REASON_I18N_KEYS))
    def test_every_reason_has_an_i18n_key(self, reason):
        assert device.device_reason_i18n_key(reason).startswith("transcription_device_")

    def test_an_unknown_reason_still_resolves_to_a_real_key(self):
        assert device.device_reason_i18n_key("something_new") == (
            device.DEVICE_REASON_I18N_KEYS[device.REASON_CPU_REQUESTED]
        )


class TestComputeType:
    def test_cpu_is_int8(self):
        assert device.select_compute_type(device.DEVICE_CPU, _probe()) == device.COMPUTE_INT8

    @pytest.mark.parametrize("capability", [(12, 0), (12, 1), (13, 0)])
    def test_sm_120_and_newer_never_get_int8(self, capability):
        # The whole reason this module exists: CTranslate2's sm_120 build has
        # INT8 disabled, and every int8 variant dies at model load.
        compute = device.select_compute_type(
            device.DEVICE_CUDA, _cuda_probe(capability=capability)
        )
        assert "int8" not in compute
        assert compute == device.COMPUTE_FLOAT16
        assert device.gpu_supports_int8(capability) is False

    @pytest.mark.parametrize("capability", [(7, 0), (7, 5), (8, 6), (8, 9), (9, 0)])
    def test_modern_gpus_get_float16(self, capability):
        assert device.select_compute_type(
            device.DEVICE_CUDA, _cuda_probe(capability=capability)
        ) == device.COMPUTE_FLOAT16

    @pytest.mark.parametrize("capability", [(3, 5), (5, 0), (6, 1)])
    def test_pre_volta_gpus_fall_back_to_float32(self, capability):
        assert device.select_compute_type(
            device.DEVICE_CUDA, _cuda_probe(capability=capability)
        ) == device.COMPUTE_FLOAT32

    @pytest.mark.parametrize("candidate", ["int8", "int8_float16", "int8_bfloat16"])
    def test_the_gate_strips_every_int8_flavour_on_sm_120(self, candidate):
        # The gate select_compute_type() actually runs its answer through,
        # tested on the variants a future VRAM-saving change would reach for.
        assert device._int8_safe(candidate, (12, 0)) == device.COMPUTE_FLOAT16
        assert device._int8_safe(candidate, None) == device.COMPUTE_FLOAT16
        assert device._int8_safe(candidate, (8, 6)) == candidate

    def test_the_cuda_path_actually_runs_through_the_gate(self, monkeypatch):
        # The wiring, not the outcome. Every value-based assertion in this
        # class stays green with the _int8_safe() call deleted, because the GPU
        # path picks float16 either way today — so the one thing that keeps the
        # gate on the path is checking that it is called.
        seen = []

        def _record(compute, capability):
            seen.append((compute, capability))
            return device.COMPUTE_FLOAT16

        monkeypatch.setattr(device, "_int8_safe", _record)
        chosen = device.select_compute_type(
            device.DEVICE_CUDA, _cuda_probe(capability=(12, 0))
        )
        assert seen == [(device.COMPUTE_FLOAT16, (12, 0))]
        assert chosen == device.COMPUTE_FLOAT16

    def test_the_cpu_path_does_not_need_the_gate(self, monkeypatch):
        # The CPU answer is int8 and must stay int8: the gate is about GPU
        # kernels, and running the CPU choice through it would silence the one
        # place int8 is correct.
        monkeypatch.setattr(device, "_int8_safe", lambda *_: "SHOULD NOT BE CALLED")
        assert device.select_compute_type(device.DEVICE_CPU, _probe()) == (
            device.COMPUTE_INT8
        )

    def test_the_gate_leaves_a_float_compute_type_alone(self):
        assert device._int8_safe(device.COMPUTE_FLOAT16, (12, 0)) == device.COMPUTE_FLOAT16
        assert device._int8_safe(device.COMPUTE_FLOAT32, (6, 1)) == device.COMPUTE_FLOAT32

    def test_an_unknown_capability_is_treated_as_modern_but_not_int8(self):
        probe = _cuda_probe(capability=None)
        assert device.select_compute_type(device.DEVICE_CUDA, probe) == device.COMPUTE_FLOAT16
        assert device.gpu_supports_int8(None) is False

    def test_no_gpu_path_ever_returns_an_int8_variant(self):
        # Swept over VRAM as well as capability. A cramped card is exactly the
        # condition under which someone would later make this prefer
        # int8_float16 — the case _int8_safe() exists for — so a sweep that
        # only varies capability would stay green with the gate deleted.
        for major in range(3, 14):
            for minor in (0, 5, 9):
                for free_vram_mb in (256, 1024, 4096, 24576):
                    probe = _cuda_probe(capability=(major, minor),
                                        free_vram_mb=free_vram_mb)
                    compute = device.select_compute_type(device.DEVICE_CUDA, probe)
                    assert "int8" not in compute or (major, minor) < (12, 0), (
                        f"sm_{major}{minor} with {free_vram_mb} MB free got {compute}"
                    )


class TestAutoSelectModel:
    def test_picks_the_largest_model_the_gpu_can_hold(self):
        chosen = device.auto_select_model(
            _cuda_probe(free_vram_mb=8192), device.DEVICE_CUDA, installed_ids=()
        )
        assert chosen == "large-v3"

    def test_never_picks_a_model_that_does_not_fit(self):
        for free_vram_mb in (512, 1024, 2048, 4096, 6144, 8192, 24576):
            probe = _cuda_probe(free_vram_mb=free_vram_mb)
            chosen = device.auto_select_model(probe, device.DEVICE_CUDA, installed_ids=())
            if chosen is None:
                continue
            model = model_catalog.get_model(chosen)
            assert model.min_vram_mb < free_vram_mb, (
                f"{chosen} needs {model.min_vram_mb} MB and only {free_vram_mb} MB is free"
            )
            assert device.model_fits(model, free_vram_mb, device.DEVICE_CUDA)

    def test_nothing_fits_returns_none(self):
        # The caller turns this into INSUFFICIENT_VRAM/RAM; guessing a model
        # that cannot load would fail later and less clearly.
        probe = _cuda_probe(free_vram_mb=256)
        assert device.auto_select_model(probe, device.DEVICE_CUDA, installed_ids=()) is None

    def test_prefers_an_installed_model_over_a_bigger_download(self):
        # 3 GB nobody asked for is worse than transcribing with what is here.
        probe = _cuda_probe(free_vram_mb=24576)
        chosen = device.auto_select_model(probe, device.DEVICE_CUDA, installed_ids=["base"])
        assert chosen == "base"

    def test_prefers_the_largest_installed_model_that_fits(self):
        probe = _cuda_probe(free_vram_mb=24576)
        chosen = device.auto_select_model(
            probe, device.DEVICE_CUDA, installed_ids=["tiny", "small", "base"]
        )
        assert chosen == "small"

    def test_an_installed_model_that_does_not_fit_is_skipped(self):
        probe = _cuda_probe(free_vram_mb=2560)
        chosen = device.auto_select_model(
            probe, device.DEVICE_CUDA, installed_ids=["large-v3"]
        )
        assert chosen != "large-v3"
        assert model_catalog.get_model(chosen).min_vram_mb <= 2048

    def test_the_cpu_path_is_measured_against_ram_not_vram(self):
        # A machine with a big card and little RAM must not be handed large-v3
        # because the VRAM figure said so.
        probe = device.HardwareProbe(total_ram_mb=4096, total_vram_mb=24576,
                                     free_vram_mb=24576)
        chosen = device.auto_select_model(probe, device.DEVICE_CPU, installed_ids=())
        assert model_catalog.get_model(chosen).min_ram_mb <= 3072

    def test_unknown_memory_uses_an_installed_model_or_nothing(self):
        probe = _probe(driver_error="nvml: could not load nvml.dll")
        assert device.auto_select_model(probe, device.DEVICE_CPU, installed_ids=()) is None
        assert device.auto_select_model(
            probe, device.DEVICE_CPU, installed_ids=["medium", "tiny"]
        ) == "tiny"

    def test_a_caller_supplied_catalogue_is_ordered_by_this_module(self):
        # Reversed on purpose: "largest that fits" must not depend on the order
        # the caller happened to hand over.
        catalog = tuple(reversed(model_catalog.list_models()))
        chosen = device.auto_select_model(
            _cuda_probe(free_vram_mb=8192), device.DEVICE_CUDA,
            installed_ids=(), catalog=catalog,
        )
        assert chosen == "large-v3"

    def test_headroom_is_required_not_just_a_bare_fit(self):
        # The rule spelled out rather than borrowed back from model_fits(): a
        # model fits when the free memory covers its minimum plus 25%. 2048 MB
        # free against a minimum of exactly 2048 MB is a miss — the minimum
        # covers the model, not Chromium and the desktop beside it.
        small = model_catalog.get_model("small")
        assert small.min_vram_mb == 2048
        assert not device.model_fits(small, 2048, device.DEVICE_CUDA)
        assert not device.model_fits(small, int(2048 * 1.25) - 1, device.DEVICE_CUDA)
        assert device.model_fits(small, int(2048 * 1.25), device.DEVICE_CUDA)

    def test_no_chosen_model_ever_breaks_the_headroom_rule(self):
        for free_vram_mb in (900, 1280, 2000, 2560, 5120, 7680, 24576):
            chosen = device.auto_select_model(
                _cuda_probe(free_vram_mb=free_vram_mb), device.DEVICE_CUDA,
                installed_ids=(),
            )
            if chosen is None:
                continue
            assert free_vram_mb >= model_catalog.get_model(chosen).min_vram_mb * 1.25

    def test_free_vram_wins_over_total_vram(self):
        probe = device.HardwareProbe(
            cuda_available=True, cuda_device_count=1, compute_capability=(8, 6),
            total_vram_mb=24576, free_vram_mb=1024,
        )
        assert device.available_memory_mb(probe, device.DEVICE_CUDA) == 1024

    def test_available_ram_wins_over_total_ram(self):
        # 16 GB installed, 11 GB already taken by Chromium/WhatsApp Web, the
        # screen reader and the user own work. Planning against the 16 buys a
        # 3 GB download and then an allocation error.
        probe = device.HardwareProbe(total_ram_mb=16384, available_ram_mb=5120)
        assert device.available_memory_mb(probe, device.DEVICE_CPU) == 5120
        chosen = device.auto_select_model(probe, device.DEVICE_CPU, installed_ids=())
        assert chosen != "large-v3"
        assert model_catalog.get_model(chosen).min_ram_mb * 1.25 <= 5120

    def test_total_ram_is_the_fallback_when_available_is_unknown(self):
        probe = device.HardwareProbe(total_ram_mb=16384)
        assert device.available_memory_mb(probe, device.DEVICE_CPU) == 16384


class TestProbeHardware:
    def test_probing_never_raises_and_answers_with_a_probe(self):
        # It loads a driver DLL and calls into the Windows memory API; on a
        # runner with neither, every field is simply unknown.
        probe = device.probe_hardware()
        assert isinstance(probe, device.HardwareProbe)
        assert isinstance(probe.cuda_available, bool)
        assert probe.cuda_device_count >= 0

    def test_a_failing_nvml_probe_still_returns_a_probe(self, monkeypatch):
        # A runner with no GPU never reaches _probe_nvml() at all, so the
        # failure has to be planted on both halves: a backend that reports a
        # device, and a driver probe that then blows up on it. That is the real
        # shape of the fault — the DLL loads and faults inside ctypes.
        monkeypatch.setitem(
            sys.modules, "ctranslate2",
            types.SimpleNamespace(get_cuda_device_count=lambda: 1),
        )

        def _boom():
            raise OSError("nvml exploded")

        monkeypatch.setattr(device, "_probe_nvml", _boom)
        probe = device.probe_hardware()
        assert isinstance(probe, device.HardwareProbe)
        assert probe.cuda_available is True
        assert probe.compute_capability is None
        # The GPU was counted and could not be described: this is the one case
        # that may be announced as a driver fault.
        assert "nvml exploded" in probe.cuda_probe_error
        assert device.resolve_device(device.PREFERENCE_CUDA, probe)[1] == (
            device.REASON_CUDA_SELECTED
        )

    def test_a_failing_ram_probe_still_returns_a_probe(self, monkeypatch):
        def _boom():
            raise OSError("GlobalMemoryStatusEx exploded")

        monkeypatch.setattr(device, "_probe_ram_mb", _boom)
        probe = device.probe_hardware()
        assert probe.total_ram_mb is None
        assert probe.available_ram_mb is None
        assert "GlobalMemoryStatusEx exploded" in probe.driver_error

    def test_a_missing_ctranslate2_still_returns_a_probe(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _refuse(name, *args, **kwargs):
            if name == "ctranslate2":
                raise ImportError("no ctranslate2 here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _refuse)
        probe = device.probe_hardware()
        assert probe.cuda_available is False
        # It lands in the aggregate, and nowhere near the driver-fault field.
        assert "ctranslate2" in probe.driver_error
        assert probe.cuda_probe_error is None

    def test_a_reported_nvml_failure_reaches_both_error_fields(self, monkeypatch):
        # The common real case: a card is present, nvml.dll is not. The helper
        # returns the reason rather than raising, and the reason has to land in
        # cuda_probe_error (which may be announced) AND in the aggregate (which
        # is what the log gets).
        monkeypatch.setitem(
            sys.modules, "ctranslate2",
            types.SimpleNamespace(get_cuda_device_count=lambda: 1),
        )
        monkeypatch.setattr(
            device, "_probe_nvml", lambda: (None, None, None, "nvml: not found")
        )
        probe = device.probe_hardware()
        assert probe.cuda_probe_error == "nvml: not found"
        assert "nvml: not found" in probe.driver_error
        assert probe.total_vram_mb is None and probe.free_vram_mb is None

    def test_loading_nvml_reports_every_path_it_tried(self, monkeypatch):
        # Without a card there is no DLL to load, so the failure path is the
        # only one testable here — and it is the one that reaches the log.
        def _refuse(path):
            raise OSError(f"cannot load {path}")

        monkeypatch.setattr(device.ctypes, "CDLL", _refuse)
        library, error = device._load_nvml()
        assert library is None
        # Keeping only the last failure left every card-less machine blaming
        # the legacy NVSMI path, the least informative of the three.
        assert "nvml.dll" in error
        assert error.count("cannot load") == len(device._NVML_LIBRARY_PATHS)

    def test_the_nvml_search_starts_with_the_bare_name(self):
        paths = device._NVML_LIBRARY_PATHS
        assert len(paths) == 3
        assert paths[0] == "nvml.dll"
        # Then the two absolute locations pynvml itself falls back to.
        assert paths[1].lower().endswith(r"system32\nvml.dll")
        assert "NVSMI" in paths[2]

    def test_a_probe_result_still_yields_a_usable_decision(self):
        probe = device.probe_hardware()
        chosen, reason = device.resolve_device(device.PREFERENCE_AUTO, probe)
        assert chosen in (device.DEVICE_CPU, device.DEVICE_CUDA)
        assert reason in device.DEVICE_REASON_I18N_KEYS
        assert device.select_compute_type(chosen, probe) in (
            device.COMPUTE_INT8, device.COMPUTE_FLOAT16, device.COMPUTE_FLOAT32,
        )


class TestErrors:
    def test_the_exception_carries_the_code(self):
        exc = errors.TranscriptionError(errors.FFMPEG_FAILED, "ffmpeg exited with 1")
        assert exc.code == errors.FFMPEG_FAILED
        assert exc.i18n_key == "transcription_error_ffmpeg_failed"

    def test_the_technical_detail_is_optional(self):
        exc = errors.TranscriptionError(errors.CANCELLED)
        assert exc.detail is None
        assert str(exc) == errors.CANCELLED

    def test_the_detail_reaches_the_log_line(self):
        exc = errors.TranscriptionError(errors.BACKEND_ERROR, "CUDA out of memory")
        assert exc.log_line == f"{errors.BACKEND_ERROR}: CUDA out of memory"

    def test_neither_str_nor_args_leaks_the_detail(self):
        # wx.MessageBox(str(exc), ...) is an idiom this repository already uses
        # (client/ui/media_viewer.py), so whatever __str__ returns is one
        # careless handler away from a screen reader spelling out a file path.
        exc = errors.TranscriptionError(errors.BACKEND_ERROR, r"C:\Users\name\file.ogg")
        assert str(exc) == errors.BACKEND_ERROR
        assert "file.ogg" not in str(exc)
        assert exc.args == (errors.BACKEND_ERROR,)
        assert "file.ogg" not in "".join(str(arg) for arg in exc.args)

    def test_the_log_line_is_just_the_code_without_a_detail(self):
        assert errors.TranscriptionError(errors.CANCELLED).log_line == errors.CANCELLED

    def test_every_error_answers_moved_even_when_no_move_was_involved(self):
        # move_models() attaches the ids it managed to move before failing, but
        # only on the path that got that far. A caller reading exc.moved after a
        # move would hit AttributeError on every other failure of the same call
        # — the busy-folder one above all, which raises its own error.
        assert errors.TranscriptionError(errors.MODELS_BUSY).moved == ()
        assert errors.TranscriptionError(errors.BACKEND_ERROR, "detail").moved == ()

    def test_every_case_the_issue_names_has_a_code(self):
        for code in (
            errors.MODEL_NOT_INSTALLED, errors.MODEL_CORRUPTED, errors.NO_DISK_SPACE,
            errors.CUDA_UNAVAILABLE, errors.INSUFFICIENT_VRAM, errors.INSUFFICIENT_RAM,
            errors.UNSUPPORTED_AUDIO_FORMAT, errors.FFMPEG_FAILED,
            errors.AUDIO_INCOMPLETE, errors.MEDIA_NOT_DOWNLOADED, errors.CANCELLED,
            errors.SAVE_FAILED, errors.BACKEND_MISSING, errors.MODEL_DOWNLOAD_FAILED,
            errors.BACKEND_ERROR,
        ):
            assert code in errors.ERROR_CODES

    def test_codes_are_unique(self):
        assert len(set(errors.ERROR_CODES)) == len(errors.ERROR_CODES)

    @pytest.mark.parametrize("code", errors.ERROR_CODES)
    def test_every_code_maps_to_an_i18n_key(self, code):
        key = errors.error_i18n_key(code)
        assert key == f"transcription_error_{code}"
        assert errors.ERROR_I18N_KEYS[code] == key

    def test_an_unknown_code_resolves_to_the_internal_error_message(self):
        # I18n.t() falls back to the raw key name, so a made-up key would have
        # the screen reader read "transcription_error_whatever" aloud.
        assert errors.error_i18n_key("whatever") == errors.ERROR_I18N_KEYS[errors.BACKEND_ERROR]
        assert errors.TranscriptionError(None).i18n_key in errors.ERROR_I18N_KEYS.values()


class TestTranslations:
    """Same rule as tests/test_language_files_in_sync.py, aimed at these keys.

    The sync test would only notice a key missing from *some* locales; a key
    missing from all five agrees with itself perfectly and still reaches the
    user as its own name.
    """

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_error_code_is_translated_everywhere(self, locale):
        table = _load(locale)
        missing = sorted(
            errors.error_i18n_key(code) for code in errors.ERROR_CODES
            if errors.error_i18n_key(code) not in table
        )
        assert missing == [], f"{locale}.json would speak these key names aloud: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_device_reason_is_translated_everywhere(self, locale):
        table = _load(locale)
        missing = sorted(
            key for key in device.DEVICE_REASON_I18N_KEYS.values() if key not in table
        )
        assert missing == [], f"{locale}.json is missing device reasons: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_size_class_is_translated_everywhere(self, locale):
        table = _load(locale)
        missing = sorted(
            key for key in model_catalog.SIZE_CLASS_I18N_KEYS.values() if key not in table
        )
        assert missing == [], f"{locale}.json is missing size labels: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_transcription_string_is_blank(self, locale):
        table = _load(locale)
        blank = sorted(
            key for key, text in table.items()
            if key.startswith("transcription_") and not text.strip()
        )
        assert blank == []

    def test_size_class_keys_cover_every_class_in_the_catalogue(self):
        for model in model_catalog.MODELS:
            assert model_catalog.size_class_i18n_key(model.size_class)

    def test_an_unknown_size_class_has_no_key(self):
        assert model_catalog.size_class_i18n_key("enormous") is None


def test_the_backend_is_never_imported_at_module_level():
    """The app has to start on a machine with no faster-whisper installed.

    The menu item that offers to install it lives in the same process, so a
    top-level import would make the feature unreachable exactly where it is
    needed — and would take the conversation window down with it. Checked
    against the source rather than sys.modules, because probe_hardware() legally
    imports ctranslate2 inside a function and would mask the difference.
    """
    from core.transcription import __file__ as package_file

    package_dir = os.path.dirname(package_file)
    offenders = []
    for name in sorted(os.listdir(package_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(package_dir, name), "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in tree.body:  # top level only — nested imports are the rule
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                if imported.split(".")[0] in ("faster_whisper", "ctranslate2", "wx"):
                    offenders.append(f"{name}: {imported}")
    assert offenders == [], f"imported at module level: {offenders}"
