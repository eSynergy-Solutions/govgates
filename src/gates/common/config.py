"""Run configuration. Every catalog/schema name comes from here, never hard-coded."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from gates.common.contract import rls_tag_key, tag_keys


@dataclass(frozen=True)
class GateConfig:
    catalog: str
    raw_schema: str = "raw"
    gov_schema: str = "gov"
    ref_schema: str = "ref"
    curated_schema: str = "curated"
    tag_prefix: str = "gg"
    rls_term_id: str = "region_code"
    hydration_principal: str = ""

    @property
    def sensitivity_tag(self) -> str:
        return tag_keys(self.tag_prefix)[0]

    @property
    def business_term_tag(self) -> str:
        return tag_keys(self.tag_prefix)[1]

    @property
    def rls_tag(self) -> str:
        return rls_tag_key(self.tag_prefix)

    def raw(self, table: str) -> str:
        return f"{self.catalog}.{self.raw_schema}.{table}"

    def gov(self, table: str) -> str:
        return f"{self.catalog}.{self.gov_schema}.{table}"

    def ref(self, table: str) -> str:
        return f"{self.catalog}.{self.ref_schema}.{table}"

    def curated(self, table: str) -> str:
        return f"{self.catalog}.{self.curated_schema}.{table}"

    def substitutions(self) -> dict[str, str]:
        return {
            "catalog": self.catalog,
            "raw_schema": self.raw_schema,
            "gov_schema": self.gov_schema,
            "ref_schema": self.ref_schema,
            "curated_schema": self.curated_schema,
        }

    @staticmethod
    def add_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--catalog", required=True)
        p.add_argument("--raw-schema", default="raw")
        p.add_argument("--gov-schema", default="gov")
        p.add_argument("--ref-schema", default="ref")
        p.add_argument("--curated-schema", default="curated")
        p.add_argument("--tag-prefix", default="gg")
        p.add_argument("--rls-term-id", default="region_code")
        p.add_argument("--hydration-principal", default="")

    @classmethod
    def from_args(cls, a: argparse.Namespace) -> GateConfig:
        return cls(a.catalog, a.raw_schema, a.gov_schema, a.ref_schema, a.curated_schema, a.tag_prefix,
                   getattr(a, "rls_term_id", "region_code"), getattr(a, "hydration_principal", ""))
