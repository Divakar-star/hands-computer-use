"""Scripted 'models' for key-free runs and tests. Their evidence is labelled scripted
and is NOT the required real LLM discovery run."""
from __future__ import annotations

from .llm import ScriptedModel

SAVINGS_BALANCE_GOAL = ("Look up member 12345 in the member servicing console and read their savings "
                        "account's available balance.")


def savings_balance_script(member: str = "12345") -> ScriptedModel:
    return ScriptedModel([
        {"tool": "click", "find": {"role": "link", "name": "Member Inquiry"}, "rationale": "open the member lookup screen"},
        {"tool": "type_text", "find": {"role": "textbox", "name": "Member Number"}, "text": member,
         "rationale": "enter the member number"},
        {"tool": "click", "find": {"role": "link", "name": "Search"}, "rationale": "run the search"},
        {"tool": "click", "find": {"role": "link", "name": "Share Savings"}, "rationale": "open the savings account"},
        {"tool": "extract_value", "name": "available_balance", "label": "Available Balance", "type": "decimal",
         "description": "Available balance of the member's savings account in USD",
         "rationale": "read the available balance"},
        {"tool": "finish", "success": True, "summary": "Read the savings available balance.",
         "capability": {"slug": "member_savings_balance",
                        "description": "Look up a credit-union member by member number and return the available "
                                       "balance of their savings account. Read-only."},
         "parameters": [{"name": "member_id", "description": "5-digit member number", "type": "string",
                         "example_value": member, "pattern": r"\d{5}", "sensitivity": "identifier"}],
         "needed_steps": [1, 2, 3, 4, 5],
         "conditional_steps": [{"step": 4, "outcome_code": "no_savings_account",
                                "description": "The member has no savings account, so there is no balance to read."}],
         "success_evidence": ["Available Balance"],
         "rationale": "goal achieved"},
    ])


OPEN_REVIEW_GOAL = ("Start opening a new Share Savings sub-account for member 12345 with a $25.00 initial deposit, "
                    "nickname 'Vacation', funded from the existing share draft, and get as far as the confirmation "
                    "review screen. Do NOT submit it.")


def open_review_script(member: str = "12345") -> ScriptedModel:
    return ScriptedModel([
        {"tool": "click", "find": {"role": "link", "name": "Open Sub-Account"}, "rationale": "open the new sub-account form"},
        {"tool": "type_text", "find": {"role": "textbox", "name": "Member Number"}, "text": member, "rationale": "member"},
        {"tool": "select_option", "find": {"role": "combobox", "name": "Account Type"}, "option": "Share Savings",
         "rationale": "product"},
        {"tool": "type_text", "find": {"role": "textbox", "name": "Initial Deposit"}, "text": "25.00", "rationale": "deposit"},
        {"tool": "type_text", "find": {"role": "textbox", "name": "Nickname (optional)"}, "text": "Vacation",
         "rationale": "nickname"},
        {"tool": "set_checked", "find": {"role": "radio", "name": "Existing share draft"}, "checked": True,
         "rationale": "funding source"},
        {"tool": "set_checked", "find": {"role": "checkbox", "name": "Member consent and disclosures provided"},
         "checked": True, "rationale": "consent was obtained by the caller"},
        {"tool": "click", "find": {"role": "button", "name": "Continue >>"}, "rationale": "go to the review screen"},
        {"tool": "finish", "success": True, "summary": "Reached the confirmation review screen without submitting.",
         "capability": {"slug": "open_subaccount_to_review",
                        "description": "Fill the Open Sub-Account form for a member and stop at the review screen "
                                       "WITHOUT submitting. Nothing is committed; a person or an approved follow-up "
                                       "capability must confirm."},
         "parameters": [
             {"name": "member_id", "description": "5-digit member number", "type": "string", "example_value": member,
              "pattern": r"\d{5}", "sensitivity": "identifier"},
             {"name": "account_type", "description": "Product to open", "type": "string",
              "example_value": "Share Savings", "sensitivity": "none"},
             {"name": "initial_deposit", "description": "Opening deposit in USD (minimum 5.00)", "type": "decimal",
              "example_value": "25.00", "sensitivity": "none"},
             {"name": "nickname", "description": "Optional label, max 20 chars", "type": "string",
              "example_value": "Vacation", "sensitivity": "none"},
             {"name": "member_consent_confirmed", "description": "Caller attests member consent/disclosures were provided",
              "type": "boolean", "example_value": "true", "sensitivity": "none"}],
         "needed_steps": [1, 2, 3, 4, 5, 6, 7, 8],
         "success_evidence": ["Confirm New Sub-Account"],
         "rationale": "reached the review screen"},
    ])
