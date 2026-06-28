"""Tests for the persistent environment model (weather + time of day).

Pure-function tests. No Simulation, no orchestrator, no LLM, no asyncio.
"""
import math

import pytest

from backend.sim.environment import (
    BASE_VISUAL_RANGE_M,
    DETECTION_DECAY,
    HEADING_SIGMA_MAX_PENALTY_DEG,
    MAST_DETECTABILITY,
    MIN_VISIBILITY,
    MIN_VISUAL_RANGE_M,
    SURFACED_DEPTH_M,
    EnvironmentConditions,
    detection_falloff,
    periscope_modifiers,
    sub_visual_exposure,
)


class TestParsing:
    def test_defaults_to_clear_day(self):
        env = EnvironmentConditions.from_mission_env(None)
        assert env.time_of_day == "day"
        assert env.weather == "clear"
        assert env.visual_visibility_factor() == pytest.approx(1.0)

    def test_empty_dict_defaults(self):
        env = EnvironmentConditions.from_mission_env({})
        assert env.time_of_day == "day"
        assert env.weather == "clear"

    def test_parses_camel_case_and_snake_case(self):
        assert EnvironmentConditions.from_mission_env(
            {"timeOfDay": "night", "weather": "fog"}
        ) == EnvironmentConditions("night", "fog")
        assert EnvironmentConditions.from_mission_env(
            {"time_of_day": "night", "weather": "fog"}
        ) == EnvironmentConditions("night", "fog")

    def test_case_insensitive(self):
        env = EnvironmentConditions.from_mission_env(
            {"timeOfDay": "NIGHT", "weather": "Fog"}
        )
        assert env == EnvironmentConditions("night", "fog")

    def test_calm_is_alias_for_clear(self):
        # Every existing mission uses weather "calm"; it must not degrade vis.
        env = EnvironmentConditions.from_mission_env({"weather": "calm"})
        assert env.visual_visibility_factor() == pytest.approx(1.0)

    def test_unknown_values_fall_back_to_clear_day(self):
        env = EnvironmentConditions.from_mission_env(
            {"timeOfDay": "eclipse", "weather": "hail"}
        )
        assert env == EnvironmentConditions("day", "clear")


class TestVisibilityFactor:
    def test_strong_severity_values(self):
        # Locks the chosen "Strong" profile.
        cases = {
            ("day", "clear"): 1.0,
            ("dawn", "clear"): 0.8,
            ("dusk", "clear"): 0.8,
            ("night", "clear"): 0.55,
            ("day", "rain"): 0.6,
            ("day", "fog"): 0.35,
        }
        for (tod, wx), expected in cases.items():
            assert EnvironmentConditions(tod, wx).visual_visibility_factor() == pytest.approx(expected)

    def test_factors_stack_multiplicatively(self):
        foggy_night = EnvironmentConditions("night", "fog").visual_visibility_factor()
        assert foggy_night == pytest.approx(0.55 * 0.35)  # ~0.19

    def test_time_ordering_day_best_night_worst(self):
        day = EnvironmentConditions("day", "clear").visual_visibility_factor()
        dusk = EnvironmentConditions("dusk", "clear").visual_visibility_factor()
        night = EnvironmentConditions("night", "clear").visual_visibility_factor()
        assert day > dusk > night

    def test_weather_ordering_clear_best_fog_worst(self):
        clear = EnvironmentConditions("day", "clear").visual_visibility_factor()
        rain = EnvironmentConditions("day", "rain").visual_visibility_factor()
        fog = EnvironmentConditions("day", "fog").visual_visibility_factor()
        assert clear > rain > fog

    def test_never_below_floor(self):
        # Even the worst combination is clamped to the visibility floor.
        worst = EnvironmentConditions("night", "fog").visual_visibility_factor()
        assert worst >= MIN_VISIBILITY


class TestPeriscopeModifiers:
    def test_clear_day_is_a_noop(self):
        mods = periscope_modifiers(EnvironmentConditions("day", "clear"))
        assert mods.visibility == pytest.approx(1.0)
        assert mods.max_range_m == pytest.approx(BASE_VISUAL_RANGE_M)
        assert mods.heading_sigma_add == pytest.approx(0.0)

    def test_range_scales_with_visibility(self):
        mods = periscope_modifiers(EnvironmentConditions("night", "fog"))
        vis = EnvironmentConditions("night", "fog").visual_visibility_factor()
        assert mods.max_range_m == pytest.approx(BASE_VISUAL_RANGE_M * vis)
        # ~2.9 km vs the clear-day 15 km — a major contraction.
        assert mods.max_range_m < 3500.0

    def test_range_respects_floor(self):
        # Construct conditions whose product would fall below the range floor.
        mods = periscope_modifiers(EnvironmentConditions("night", "fog"))
        assert mods.max_range_m >= MIN_VISUAL_RANGE_M

    def test_heading_sigma_widens_in_poor_visibility(self):
        clear = periscope_modifiers(EnvironmentConditions("day", "clear"))
        foggy = periscope_modifiers(EnvironmentConditions("night", "fog"))
        assert foggy.heading_sigma_add > clear.heading_sigma_add
        # (1 - vis) * MAX_PENALTY at foggy-night vis ~0.19.
        vis = EnvironmentConditions("night", "fog").visual_visibility_factor()
        assert foggy.heading_sigma_add == pytest.approx((1.0 - vis) * HEADING_SIGMA_MAX_PENALTY_DEG)


class TestDetectionFalloff:
    def test_point_blank_is_certain(self):
        assert detection_falloff(0.0, 15000.0) == pytest.approx(1.0)

    def test_monotonically_decreasing_with_distance(self):
        prev = 2.0
        for d in range(0, 16000, 1000):
            val = detection_falloff(float(d), 15000.0)
            assert val < prev
            prev = val

    def test_residual_at_visibility_range(self):
        # By construction, ~e^(-DETECTION_DECAY) of contrast remains at max range.
        assert detection_falloff(15000.0, 15000.0) == pytest.approx(math.exp(-DETECTION_DECAY))

    def test_is_exponential_not_linear(self):
        # Exponential decay is convex: at mid-range it sits BELOW the linear
        # ramp (1 - r/R = 0.5), i.e. detection drops off faster than linear.
        mid = detection_falloff(7500.0, 15000.0)
        assert mid < 0.5
        assert mid == pytest.approx(math.exp(-DETECTION_DECAY * 0.5))

    def test_shorter_visibility_range_steepens_decay(self):
        # At a fixed distance, fog (small range) detects far less than clear air.
        d = 2000.0
        clear = detection_falloff(d, 15000.0)
        fog = detection_falloff(d, 2888.0)
        assert fog < clear

    def test_zero_or_negative_range_is_zero(self):
        assert detection_falloff(1000.0, 0.0) == 0.0
        assert detection_falloff(1000.0, -5.0) == 0.0


class TestVisualExposure:
    def test_surfaced_hull_fully_exposed(self):
        exp = sub_visual_exposure(depth_m=0.0, periscope_up=False, radio_up=False)
        assert exp.exposed is True
        assert exp.detectability == pytest.approx(1.0)
        assert exp.mode == "surface"

    def test_just_below_surface_threshold_still_surfaced(self):
        exp = sub_visual_exposure(SURFACED_DEPTH_M, periscope_up=False, radio_up=False)
        assert exp.exposed is True
        assert exp.detectability == pytest.approx(1.0)

    def test_deep_and_masts_down_is_invisible(self):
        exp = sub_visual_exposure(depth_m=60.0, periscope_up=False, radio_up=False)
        assert exp.exposed is False
        assert exp.detectability == pytest.approx(0.0)
        assert exp.mode == "none"

    def test_periscope_up_at_depth_exposes_as_thin_mast(self):
        exp = sub_visual_exposure(depth_m=18.0, periscope_up=True, radio_up=False)
        assert exp.exposed is True
        assert exp.detectability == pytest.approx(MAST_DETECTABILITY)
        assert exp.mode == "periscope"

    def test_radio_mast_up_at_depth_exposes(self):
        exp = sub_visual_exposure(depth_m=18.0, periscope_up=False, radio_up=True)
        assert exp.exposed is True
        assert exp.detectability == pytest.approx(MAST_DETECTABILITY)

    def test_mast_is_harder_to_spot_than_surfaced_hull(self):
        surfaced = sub_visual_exposure(0.0, False, False)
        masts = sub_visual_exposure(18.0, True, True)
        assert masts.detectability < surfaced.detectability

    def test_surfaced_dominates_even_with_masts_up(self):
        # At/near surface the hull is the giveaway regardless of masts.
        exp = sub_visual_exposure(depth_m=2.0, periscope_up=True, radio_up=True)
        assert exp.detectability == pytest.approx(1.0)
        assert exp.mode == "surface"


class TestProjection:
    def test_to_dict_shape(self):
        d = EnvironmentConditions("night", "fog").to_dict()
        assert d["timeOfDay"] == "night"
        assert d["weather"] == "fog"
        assert d["visibility"] == pytest.approx(0.193, abs=0.01)
        assert "fog" in d["label"].lower()
        assert "night" in d["label"].lower()

    def test_label_clear_reads_clear(self):
        assert EnvironmentConditions("day", "clear").label() == "Day, clear"
        assert EnvironmentConditions("day", "calm").label() == "Day, clear"

    def test_conditions_are_immutable(self):
        env = EnvironmentConditions("night", "fog")
        with pytest.raises(Exception):
            env.weather = "clear"  # frozen dataclass
