"""Synthetic table specs.

Each physical column records the glossary term it was generated from (or None for a
deliberate no-match column). The golden set is derived from these specs, so ground truth
and data can never drift apart.

Difficulty labels:
  easy   - name close to the term
  abbrev - abbreviated / variant spelling
  hard   - ambiguous, misleading, or needs domain knowledge
  none   - no correct term exists
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Col:
    name: str
    term: str | None
    difficulty: str
    kind: str | None = None  # generator for no-match columns
    sens: str | None = None  # expected sensitivity override for no-match columns


@dataclass(frozen=True)
class Table:
    name: str
    rows: int
    pk: str | None
    cols: tuple[Col, ...]
    locale: str = "en_GB"
    region: str | None = None  # fixed region; None = mixed UK/US/AU
    id_prefix: str | None = None  # prefix for fresh primary keys
    tags: dict = field(default_factory=dict)


def C(name, term, difficulty="abbrev", kind=None, sens=None) -> Col:
    return Col(name, term, difficulty, kind, sens)


def NM(name, kind, sens="internal") -> Col:
    return Col(name, None, "none", kind, sens)


TABLES: tuple[Table, ...] = (
    Table("clients", 3000, "client_identifier", (
        C("cust_id", "client_identifier"),
        C("cust_nm", "client_full_name"),
        C("dob", "date_of_birth"),
        C("ni_no", "national_insurance_number"),
        C("email_addr", "email_address"),
        C("ph_num", "phone_number"),
        C("addr_line1", "postal_address"),
        C("postcode", "postcode", "easy"),
        C("rgn_cd", "region_code"),
        C("client_type", "client_type", "easy"),
        C("client_since", "client_since_date", "easy"),
        C("mktg_consent", "marketing_consent_flag"),
        C("vuln_flag", "vulnerable_customer_flag", "hard"),
        C("occupation", "occupation", "easy"),
        NM("etl_batch_id", "batch_id"),
        NM("filler_01", "blank", sens="internal"),
    ), id_prefix="C"),
    Table("brokers", 150, "broker_code", (
        C("brk_cd", "broker_code"),
        C("brk_name", "broker_name"),
        C("fca_ref", "fca_firm_reference"),
        C("contact_email", "broker_contact_email", "hard"),
        C("broker_tier", "broker_tier", "easy"),
        C("onboard_dt", "broker_onboarding_date"),
        C("rgn_cd", "region_code"),
    ), id_prefix="BRK"),
    Table("underwriters", 60, "underwriter_identifier", (
        C("uw_id", "underwriter_identifier"),
        C("uw_name", "underwriter_name"),
        C("auth_limit", "underwriting_authority_limit"),
        C("team", "underwriting_team", "hard"),
        C("rgn_cd", "region_code"),
        NM("misc_txt", "words"),
    ), id_prefix="UW"),
    Table("policies", 6000, "policy_reference", (
        C("pol_ref", "policy_reference"),
        C("cust_id", "client_identifier"),
        C("lob", "policy_line_of_business", "hard"),
        C("incept_dt", "policy_inception_date"),
        C("exp_dt", "policy_expiry_date"),
        C("gwp_gbp", "gross_written_premium"),
        C("nwp", "net_written_premium", "hard"),
        C("pol_status", "policy_status"),
        C("ccy", "policy_currency", "hard"),
        C("sum_ins", "sum_insured"),
        C("xs_amt", "deductible_amount", "hard"),
        C("uw_id", "underwriter_identifier"),
        C("brk_cd", "broker_code"),
        C("comm_pct", "commission_rate"),
        C("rgn_cd", "region_code"),
        C("terr", "coverage_territory", "hard"),
        NM("x_flag", "yn_random"),
        C("rec_crt_ts", "record_created_timestamp"),
    ), id_prefix="POL-"),
    Table("claims", 2500, "claim_identifier", (
        C("clm_id", "claim_identifier"),
        C("pol_ref", "policy_reference"),
        C("loss_dt", "date_of_loss"),
        C("notif_dt", "claim_notification_date"),
        C("clm_amt", "claim_amount"),
        C("rsv_amt", "claim_reserve_amount"),
        C("paid_amt", "claim_paid_amount"),
        C("status_cd", "claim_status", "hard"),
        C("col_cd", "cause_of_loss", "hard"),
        C("hdlr_id", "claim_handler_identifier"),
        C("notes_txt", "claim_description", "hard"),
        C("loss_loc", "loss_location"),
        C("lit_ind", "litigation_flag"),
        C("cat_cd", "catastrophe_code", "hard"),
        C("rgn_cd", "region_code"),
        NM("chk_sum", "hex8"),
        NM("tmp_col", "int_noise"),
    ), id_prefix="CLM-"),
    Table("payments", 8000, "payment_identifier", (
        C("pmt_id", "payment_identifier"),
        C("pol_ref", "policy_reference"),
        C("sort_code", "sort_code", "easy"),
        C("acct_last4", "bank_account_last_four"),
        C("amt", "payment_amount", "hard"),
        C("pmt_dt", "payment_date"),
        C("pmt_mthd", "payment_method"),
        C("pmt_status", "payment_status"),
        C("inv_no", "invoice_number"),
        C("fx_rate", "exchange_rate"),
        C("ccy", "currency_code"),
        C("rgn_cd", "region_code"),
        NM("rowhash", "sha"),
    ), id_prefix="PMT"),
    Table("quotes", 5000, "quote_reference", (
        C("quote_ref", "quote_reference"),
        C("quote_date", "quote_date", "easy"),
        C("cust_id", "client_identifier"),
        C("lob", "policy_line_of_business", "hard"),
        C("quoted_prem", "quoted_premium"),
        C("quote_status", "quote_status", "easy"),
        C("converted", "conversion_flag", "hard"),
        C("risk_scr", "risk_score"),
        C("referral_ind", "referral_flag"),
        C("src_sys", "source_system_code"),
        C("rgn_cd", "region_code"),
        NM("user_def_3", "mostly_null"),
    ), id_prefix="Q-"),
    # Legacy US extract: upper-case naming, different identifiers.
    Table("client_master_us", 800, "client_identifier", (
        C("CLIENT_ID", "client_identifier", "easy"),
        C("CLIENT_NAME", "client_full_name", "easy"),
        C("BIRTH_DATE", "date_of_birth", "easy"),
        C("SSN", "social_security_number"),
        C("EMAIL", "email_address", "easy"),
        C("PHONE", "phone_number", "easy"),
        C("ZIP", "postcode", "hard"),
        NM("STATE_CD", "state"),
        C("ANNUAL_REV", "annual_revenue"),
        C("EMP_CNT", "employee_count"),
        C("SIC_CD", "industry_sector_code"),
        C("LAST_UPD", "record_updated_timestamp"),
        NM("LEGACY_REF_2", "alnum"),
    ), locale="en_US", region="US", id_prefix="USC"),
    # AU claims extract: camel case, local currency in names.
    Table("claims_extract_au", 600, "claim_identifier", (
        C("ClaimNumber", "claim_identifier", "easy"),
        C("PolicyNo", "policy_reference"),
        C("DateOfLoss", "date_of_loss", "easy"),
        C("IncurredAUD", "claim_amount", "hard"),
        C("ReserveAUD", "claim_reserve_amount"),
        C("Status", "claim_status", "hard"),
        C("FraudFlag", "fraud_indicator"),
        C("TFN", "tax_file_number"),
        C("RecoveryAmt", "subrogation_amount", "hard"),
        C("SettledOn", "claim_settlement_date", "hard"),
        NM("Col17", "float_noise"),
    ), locale="en_AU", region="AU", id_prefix="AUCLM"),
)

# Tables whose region column drives row filters in the consumption gate.
RLS_TABLES = ("clients", "policies", "claims", "payments", "quotes")
