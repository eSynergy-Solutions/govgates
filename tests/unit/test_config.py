"""GateConfig: tag_prefix seam (specs/hydration.md step 7: `tag_prefix` is a bundle variable, default `gg`)."""

from __future__ import annotations

import argparse

from gates.common.config import GateConfig


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    GateConfig.add_args(p)
    return p.parse_args(argv)


def test_tag_prefix_defaults_to_gg():
    cfg = GateConfig("c")
    assert cfg.tag_prefix == "gg"
    assert cfg.sensitivity_tag == "gg_sensitivity"
    assert cfg.business_term_tag == "gg_business_term"


def test_tag_prefix_overrides_both_keys():
    cfg = GateConfig("c", tag_prefix="xx")
    assert cfg.sensitivity_tag == "xx_sensitivity"
    assert cfg.business_term_tag == "xx_business_term"


def test_positional_construction_still_works():
    cfg = GateConfig("c", "landing", "g", "r", "cur")
    assert (cfg.catalog, cfg.raw_schema, cfg.gov_schema, cfg.ref_schema, cfg.curated_schema) == (
        "c", "landing", "g", "r", "cur")
    assert cfg.raw("t") == "c.landing.t"
    assert cfg.tag_prefix == "gg"


def test_add_args_default_tag_prefix():
    a = _parse(["--catalog", "c"])
    assert a.tag_prefix == "gg"
    assert GateConfig.from_args(a).tag_prefix == "gg"


def test_from_args_passes_tag_prefix():
    cfg = GateConfig.from_args(_parse(["--catalog", "c", "--tag-prefix", "xx", "--gov-schema", "g"]))
    assert cfg.tag_prefix == "xx"
    assert cfg.gov_schema == "g"
    assert cfg.sensitivity_tag == "xx_sensitivity"


def test_same_args_same_config():
    argv = ["--catalog", "c", "--tag-prefix", "xx"]
    assert GateConfig.from_args(_parse(argv)) == GateConfig.from_args(_parse(argv))


# ---------------------------------------------------------------- row filter (specs/consumption.md row_filter)

def test_rls_tag_follows_prefix():
    assert GateConfig("c").rls_tag == "gg_rls_attribute"
    assert GateConfig("c", tag_prefix="xx").rls_tag == "xx_rls_attribute"


def test_rls_defaults():
    cfg = GateConfig("c")
    assert cfg.rls_term_id == "region_code"
    assert cfg.hydration_principal == ""


def test_rls_args_default():
    cfg = GateConfig.from_args(_parse(["--catalog", "c"]))
    assert cfg.rls_term_id == "region_code"
    assert cfg.hydration_principal == ""


def test_rls_args_round_trip():
    cfg = GateConfig.from_args(_parse(["--catalog", "c", "--rls-term-id", "sales_zone",
                                       "--hydration-principal", "ci-sp-0000", "--tag-prefix", "xx"]))
    assert cfg.rls_term_id == "sales_zone"
    assert cfg.hydration_principal == "ci-sp-0000"
    assert cfg.tag_prefix == "xx"
    assert cfg.rls_tag == "xx_rls_attribute"
