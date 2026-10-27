"""Transparent recommendation generator distinguishing evidence from advice.

This module implements the decision-support engine required by Section 9 of the
TECH5 specification, providing inspectable recommendations with explicit
criteria, mathematical evidence, and policy-oriented advice.
"""

import dataclasses
from typing import Any

from app.domain import models


@dataclasses.dataclass
class RecommendationItem:
    """An individual decision recommendation.

    Attributes:
        priority_focus: The organizational objective prioritized (e.g.
            Student Satisfaction, Project Skill Delivery, Egalitarian Balance).
        recommended_solution_id: Identifier of the top solution for this focus.
        recommended_alternative_number: 1-based index of recommended solution.
        criteria_used: Explicit list of analytical metrics evaluated.
        evidence: Objective calculated facts supporting the recommendation.
        advice: Practical normative advice describing operational trade-offs.
    """

    priority_focus: str
    recommended_solution_id: str
    recommended_alternative_number: int
    criteria_used: list[str]
    evidence: list[str]
    advice: str

    def to_dict(self) -> dict[str, Any]:
        """Serializes recommendation item to a dictionary.

        Returns:
            Dictionary containing recommendation fields.
        """
        return {
            "priority_focus": self.priority_focus,
            "recommended_solution_id": self.recommended_solution_id,
            "recommended_alternative_number": (
                self.recommended_alternative_number
            ),
            "criteria_used": list(self.criteria_used),
            "evidence": list(self.evidence),
            "advice": self.advice,
        }


def generate_recommendations(
    solutions: list[models.Solution],
) -> list[RecommendationItem]:
    """Generates policy-oriented recommendations from alternative solutions.

    Args:
        solutions: Feasible alternative solutions from an assignment run.

    Returns:
        List of structured RecommendationItem instances.
    """
    if not solutions:
        return []

    recommendations: list[RecommendationItem] = []

    # Priority 1: Maximizing Student Satisfaction
    best_pref_sol = min(
        solutions,
        key=lambda s: (
            s.indicators.get("fairness", {}).get("unranked_count", 999),
            s.indicators.get("fairness", {}).get("mean_rank", 999.0),
            s.indicators.get("fairness", {}).get("worst_rank", 999),
        ),
    )
    pref_indicators = best_pref_sol.indicators
    pref_fairness = pref_indicators.get("fairness", {})
    pref_efficiency = pref_indicators.get("efficiency", {})

    recommendations.append(
        RecommendationItem(
            priority_focus="Student Preference Maximization",
            recommended_solution_id=best_pref_sol.id,
            recommended_alternative_number=best_pref_sol.alternative_number,
            criteria_used=[
                "Average student preference rank (lower is better)",
                "Count of students assigned outside stated preferences",
                "Worst individual student rank",
            ],
            evidence=[
                f"Achieves average preference rank of "
                f"{pref_fairness.get('mean_rank', 'N/A')}.",
                f"Assigned {pref_fairness.get('unranked_count', 0)} students "
                "outside stated preferences.",
                f"Worst individual student rank is "
                f"{pref_fairness.get('worst_rank', 'N/A')}.",
                f"Maintains average skill-fit score of "
                f"{pref_efficiency.get('mean_skill_fit', 0.0):.2f}.",
            ],
            advice=(
                f"Adopt Solution {best_pref_sol.alternative_number} if "
                "student satisfaction, programme morale, and minimizing "
                "student appeals are the primary administrative goals."
            ),
        )
    )

    # Priority 2: Maximizing Project Skill Fit & Deliverable Quality
    best_skill_sol = max(
        solutions,
        key=lambda s: (
            s.indicators.get("efficiency", {}).get("mean_skill_fit", -1.0),
            -s.indicators.get("efficiency", {}).get(
                "unmet_soft_requirements_count", 999
            ),
        ),
    )
    skill_indicators = best_skill_sol.indicators
    skill_efficiency = skill_indicators.get("efficiency", {})
    skill_fairness = skill_indicators.get("fairness", {})

    recommendations.append(
        RecommendationItem(
            priority_focus="Project Skill Quality & Delivery Assurance",
            recommended_solution_id=best_skill_sol.id,
            recommended_alternative_number=best_skill_sol.alternative_number,
            criteria_used=[
                "Average student-project skill fit score (higher is better)",
                "Total unmet soft skill requirements (lower is better)",
            ],
            evidence=[
                f"Highest average skill fit score of "
                f"{skill_efficiency.get('mean_skill_fit', 0.0):.2f}.",
                f"Unmet soft requirements count: "
                f"{skill_efficiency.get('unmet_soft_requirements_count', 0)}.",
                f"Average student preference rank is "
                f"{skill_fairness.get('mean_rank', 'N/A')}.",
            ],
            advice=(
                f"Adopt Solution {best_skill_sol.alternative_number} when "
                "partner organizations or client project deliverables demand "
                "rigorous technical and domain capabilities."
            ),
        )
    )

    # Priority 3: Egalitarian Fairness & Balanced Trade-off
    best_balanced_sol = min(
        solutions,
        key=lambda s: (
            s.indicators.get("fairness", {}).get("rank_gini", 1.0),
            s.indicators.get("fairness", {}).get("rank_variance", 999.0),
            s.indicators.get("fairness", {}).get("mean_rank", 999.0),
        ),
    )
    bal_indicators = best_balanced_sol.indicators
    bal_fairness = bal_indicators.get("fairness", {})
    bal_efficiency = bal_indicators.get("efficiency", {})

    recommendations.append(
        RecommendationItem(
            priority_focus="Balanced & Egalitarian Allocation",
            recommended_solution_id=best_balanced_sol.id,
            recommended_alternative_number=best_balanced_sol.alternative_number,
            criteria_used=[
                "Rank Gini coefficient (lower is more equal)",
                "Rank variance across students (tie-breaker, lower is better)",
                "Mean preference rank (tie-breaker, lower is better)",
            ],
            evidence=[
                f"Lowest rank disparity (Gini coefficient: "
                f"{bal_fairness.get('rank_gini', 'N/A')}).",
                f"Rank variance: {bal_fairness.get('rank_variance', 'N/A')}.",
                f"Average skill fit: "
                f"{bal_efficiency.get('mean_skill_fit', 0.0):.2f}.",
                f"Average rank: {bal_fairness.get('mean_rank', 'N/A')}.",
            ],
            advice=(
                f"Adopt Solution {best_balanced_sol.alternative_number} if the "
                "academic committee seeks a fair Pareto compromise that avoids "
                "extreme individual disadvantage."
            ),
        )
    )

    return recommendations
