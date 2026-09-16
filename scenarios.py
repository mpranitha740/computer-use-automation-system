"""
Pre-declared capability contracts for discovery runs against the mock
CoreBank admin console. A human (here: the developer kicking off discovery)
declares the goal, the concrete parameter values to use for this trial run,
the typed outputs the capability must produce, and the known business
outcomes for this flow -- discovery's job is to find a real UI path that
satisfies that contract, not to invent the contract itself.
"""
from agent.schemas import (
    BusinessOutcomePattern, Checkpoint, OutputSpec, Parameter, ParamType, TargetApp,
)

BASE_URL = "http://127.0.0.1:5055"

TARGET = TargetApp(
    app_id="corebank_admin",
    vendor_product="CoreBankAdminConsole",
    base_url=f"{BASE_URL}/login",
)

LOOKUP_MEMBER_BALANCE = dict(
    capability_name="lookup_member_balance",
    goal=(
        "Log in to the CoreBank admin console as the operator (username 'operator', "
        "password 'demo-pass'), search for member 12345, open their detail page, and "
        "read their current savings balance."
    ),
    target=TARGET,
    parameters=[
        Parameter(name="username", type=ParamType.STRING, description="Operator login username",
                  example="operator", sensitive=True),
        Parameter(name="password", type=ParamType.STRING, description="Operator login password",
                  example="demo-pass", sensitive=True),
        Parameter(name="member_id", type=ParamType.STRING, description="Member ID to look up",
                  example="12345"),
    ],
    outputs=[
        OutputSpec(name="savings_balance", type=ParamType.STRING,
                   description="The member's current savings balance as displayed on the detail page"),
    ],
    success_hint="the member detail page is showing, with 'Current Savings Balance' visible",
    success_checkpoint=Checkpoint(
        description="Member detail page loaded with savings balance visible",
        expect_text_contains="Current Savings Balance",
    ),
    business_outcomes=[
        BusinessOutcomePattern(code="member_not_found", description="No member exists with the given ID",
                                match_text_contains="No member found for ID"),
        BusinessOutcomePattern(code="member_locked", description="Member record is locked/restricted",
                                match_text_contains="Access denied: member"),
    ],
)

OPEN_SUB_ACCOUNT_REACH_CONFIRMATION = dict(
    capability_name="open_sub_account_reach_confirmation",
    goal=(
        "Log in to the CoreBank admin console as the operator (username 'operator', "
        "password 'demo-pass'), open member 12345's page, start opening a new Savings "
        "sub-account with an initial deposit of 50, and reach the confirmation screen. "
        "Do NOT click the final 'Confirm & Open Account' button -- reaching the "
        "confirmation screen with the entered details visible is the whole goal."
    ),
    target=TARGET,
    parameters=[
        Parameter(name="username", type=ParamType.STRING, description="Operator login username",
                  example="operator", sensitive=True),
        Parameter(name="password", type=ParamType.STRING, description="Operator login password",
                  example="demo-pass", sensitive=True),
        Parameter(name="member_id", type=ParamType.STRING, description="Member ID to open a sub-account for",
                  example="12345"),
        Parameter(name="account_type", type=ParamType.STRING, description="Sub-account type",
                  example="Savings"),
        Parameter(name="deposit", type=ParamType.STRING, description="Initial deposit amount in dollars",
                  example="50"),
    ],
    outputs=[
        OutputSpec(name="confirmation_details", type=ParamType.STRING,
                   description="The account type and deposit amount shown on the confirmation screen"),
    ],
    success_hint="the confirmation screen is showing the entered account type and deposit, "
                 "with the 'This action is irreversible once confirmed.' notice visible",
    success_checkpoint=Checkpoint(
        description="Confirmation screen reached without submitting it",
        expect_text_contains="This action is irreversible once confirmed.",
    ),
    business_outcomes=[
        BusinessOutcomePattern(code="member_not_found", description="No member exists with the given ID",
                                match_text_contains="No member found for ID"),
        BusinessOutcomePattern(code="validation_error", description="Deposit amount failed validation",
                                match_text_contains="Validation error"),
    ],
)
