"""Small, transparent tag alias registry (companies report one concept under different XBRL tags).

Priority order matters: the first alias tag with surviving candidates wins
(fact_selection.py). A tag that a company stopped using simply has no facts in
the pinned accession and is skipped, never silently substituted.
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel


class Concept(str, Enum):
    REVENUE = "revenue"
    OPERATING_INCOME = "operating_income"
    NET_INCOME = "net_income"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    CAPEX = "capex"
    CURRENT_ASSETS = "current_assets"
    CURRENT_LIABILITIES = "current_liabilities"


class ConceptSpec(BaseModel):
    concept: Concept
    tags: tuple[str, ...]                # priority order, small and transparent
    unit: Literal["USD"]
    period_type: Literal["duration", "instant"]


CONCEPTS: dict[Concept, ConceptSpec] = {
    Concept.REVENUE: ConceptSpec(
        concept=Concept.REVENUE,
        tags=(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ),
        unit="USD",
        period_type="duration",
    ),
    Concept.OPERATING_INCOME: ConceptSpec(
        concept=Concept.OPERATING_INCOME,
        tags=("OperatingIncomeLoss",),
        unit="USD",
        period_type="duration",
    ),
    Concept.NET_INCOME: ConceptSpec(
        concept=Concept.NET_INCOME,
        tags=("NetIncomeLoss",),
        unit="USD",
        period_type="duration",
    ),
    Concept.OPERATING_CASH_FLOW: ConceptSpec(
        concept=Concept.OPERATING_CASH_FLOW,
        tags=("NetCashProvidedByUsedInOperatingActivities",),
        unit="USD",
        period_type="duration",
    ),
    Concept.CAPEX: ConceptSpec(
        concept=Concept.CAPEX,
        tags=("PaymentsToAcquirePropertyPlantAndEquipment",),  # reported as a positive outflow
        unit="USD",
        period_type="duration",
    ),
    Concept.CURRENT_ASSETS: ConceptSpec(
        concept=Concept.CURRENT_ASSETS,
        tags=("AssetsCurrent",),
        unit="USD",
        period_type="instant",
    ),
    Concept.CURRENT_LIABILITIES: ConceptSpec(
        concept=Concept.CURRENT_LIABILITIES,
        tags=("LiabilitiesCurrent",),
        unit="USD",
        period_type="instant",
    ),
}

# Fiscal-year-end month (1-12) per ticker; drives fiscal-period resolution (§3).
# A resolved period end must fall within one calendar month of this month.
FISCAL_YEAR_END_MONTH: dict[str, int] = {"MSFT": 6, "AAPL": 9}
