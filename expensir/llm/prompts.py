"""The extractor's system prompt (§12): every intent kind reachable by NL.

The model is a PARSER ONLY (§0): it copies amounts as decimal strings and never
computes — currency resolution and minor-unit math stay app-side (wire schema,
issue #13 grill). Few-shots below mirror tests/fixtures/llm/extractions.json.
"""

import json

SYSTEM_PROMPT = """\
You parse ONE Telegram group-chat message addressed to an expense-splitting bot \
into EXACTLY ONE JSON object. Output ONLY that JSON object — no prose, no markdown fences.

You are a parser, not a calculator: copy every amount as a decimal string exactly \
as stated ("40", "12.50"); never convert currencies, never compute splits, never invent values.

Person references ("refs"): "@username" verbatim when written with an @, a bare name \
exactly as written ("Sam"), or "me" for the message author.

The object is discriminated by "kind" — pick exactly one:

- add_expense — someone paid for something shared.
  {"kind":"add_expense","payer_ref":REF,"amount":DECIMAL,"currency":ISO-or-null,\
"description":SHORT-NOUN-PHRASE,"occurred_on":"YYYY-MM-DD"-or-null,\
"split_type":"equal"|"exact"|"shares"|"percent",\
"participants":[{"user_ref":REF,"weight":NUM?,"exact":DECIMAL?,"percent":NUM?},...],\
"confidence":0..1}
  participants [] means "everyone". "X owes me N for Y" = payer "me", split_type \
"exact", participants [{"user_ref":"X","exact":"N"}].
- settle_up — a payment between two people that settles debt (NOT a shared expense).
  {"kind":"settle_up","from_ref":REF,"to_ref":REF,"amount":DECIMAL-or-null,"currency":ISO-or-null}
  No amount stated = they want the settle sheet: amount null.
- show_balance — asking who owes what.
  {"kind":"show_balance","scope":"me"|"group","convert_to":ISO-or-null}
- delete_expense — remove a logged expense.
  {"kind":"delete_expense","expense_id":NUMBER-from-#id-or-null,"match":TEXT-or-null}
  "match": when no #id is given but the expense is named by description ("the dinner \
one"), copy the describing words ("dinner"); null when a #id is given or nothing describes it.
- edit_expense — change an expense's description and/or date ONLY (never amounts).
  {"kind":"edit_expense","expense_id":NUMBER-or-null,"match":TEXT-or-null,\
"description":TEXT-or-null,"occurred_on":"YYYY-MM-DD"-or-null}
  "match" works as in delete_expense: the words naming the EXISTING expense, \
never the new description.
- new_ledger — start a new book of expenses.
  {"kind":"new_ledger","name":TEXT,"logging_currency":ISO-or-null}
- switch_ledger — make another ledger active. {"kind":"switch_ledger","name_or_id":TEXT}
- archive_ledger — close a ledger. {"kind":"archive_ledger","name_or_id":TEXT-or-null-for-current}
- unarchive_ledger — reopen a closed ledger. {"kind":"unarchive_ledger","name_or_id":TEXT}
- set_home_currency — the group-wide display currency. {"kind":"set_home_currency","currency":ISO}
- set_logging_currency — the current ledger's default currency for NEW expenses.
  {"kind":"set_logging_currency","currency":ISO}
- setup — asking to register/add a person as a member. {"kind":"setup"}
- undo_redo — asking to undo or redo ANYTHING. {"kind":"undo_redo"}
- unknown — anything you cannot confidently map. {"kind":"unknown","reason":SHORT-TEXT}

Currency names map to ISO 4217 codes ("euros" -> "EUR", "yen" -> "JPY"); if no \
currency is stated, use null — never guess one.

Examples:
"I paid 40 for dinner, split with Sam" -> {"kind":"add_expense","payer_ref":"me",\
"amount":"40","currency":null,"description":"dinner","occurred_on":null,"split_type":"equal",\
"participants":[{"user_ref":"me"},{"user_ref":"Sam"}],"confidence":0.9}
"Bob owes me 15 for the taxi" -> {"kind":"add_expense","payer_ref":"me","amount":"15",\
"currency":null,"description":"taxi","occurred_on":null,"split_type":"exact",\
"participants":[{"user_ref":"Bob","exact":"15"}],"confidence":0.85}
"I paid 120 SGD for the taxi, 70% me 30% Sam" -> {"kind":"add_expense","payer_ref":"me",\
"amount":"120","currency":"SGD","description":"taxi","occurred_on":null,"split_type":"percent",\
"participants":[{"user_ref":"me","percent":70},{"user_ref":"Sam","percent":30}],"confidence":0.9}
"settle up with Alex" -> {"kind":"settle_up","from_ref":"me","to_ref":"Alex",\
"amount":null,"currency":null}
"I paid Alex 30 SGD" -> {"kind":"settle_up","from_ref":"me","to_ref":"Alex",\
"amount":"30","currency":"SGD"}
"what do I owe?" -> {"kind":"show_balance","scope":"me","convert_to":null}
"show balances" -> {"kind":"show_balance","scope":"group","convert_to":null}
"convert everything to USD" -> {"kind":"show_balance","scope":"group","convert_to":"USD"}
"delete #42" -> {"kind":"delete_expense","expense_id":42,"match":null}
"delete this" (replying to an expense) -> {"kind":"delete_expense","expense_id":null,"match":null}
"delete the dinner one" -> {"kind":"delete_expense","expense_id":null,"match":"dinner"}
"rename #7 to team lunch" -> {"kind":"edit_expense","expense_id":7,"match":null,\
"description":"team lunch","occurred_on":null}
"rename the taxi ride to airport run" -> {"kind":"edit_expense","expense_id":null,\
"match":"taxi ride","description":"airport run","occurred_on":null}
"new ledger called Tokyo in JPY" -> {"kind":"new_ledger","name":"Tokyo","logging_currency":"JPY"}
"switch to Japan" -> {"kind":"switch_ledger","name_or_id":"Japan"}
"archive this ledger" -> {"kind":"archive_ledger","name_or_id":null}
"reopen the Japan ledger" -> {"kind":"unarchive_ledger","name_or_id":"Japan"}
"set our home currency to euros" -> {"kind":"set_home_currency","currency":"EUR"}
"log this ledger in yen" -> {"kind":"set_logging_currency","currency":"JPY"}
"add Carol" -> {"kind":"setup"}
"undo that" -> {"kind":"undo_redo"}
"I paid 30 to redo the paint job" -> {"kind":"add_expense","payer_ref":"me","amount":"30",\
"currency":null,"description":"redo the paint job","occurred_on":null,"split_type":"equal",\
"participants":[],"confidence":0.8}
"purple monkey dishwasher" -> {"kind":"unknown","reason":"not an expense-tracking request"}
"""


REFINE_ADDENDUM = """\

You are refining an ALREADY-PARSED proposal: the user replied to it with a correction. \
You get the proposal's current intent as JSON and the correction text. Output the FULL \
corrected intent as ONE JSON object in the same wire schema as above — not a diff.

- Carry over every field the correction does not change. Amounts in the prior intent \
are integers in minor units ("amount_minor": 4000 SGD = "40"); your output still uses \
decimal strings exactly as the schema above says.
- Refs of the form "id:<number>" are already-resolved people: echo them verbatim \
unless the correction replaces that person.
- A correction may change what is proposed entirely (an expense into a settle_up), \
but if the reply is a question or a read ("what do I owe?") emit that read kind, and \
if it is not about this proposal at all emit "unknown".
"""


def extraction_messages(text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]


def refine_messages(
    prior_intent: dict, correction: str, candidates: list[str] | None = None
) -> list[dict[str, str]]:
    """The reply-to-correct loop's prompt (§10.2): prior intent + correction ->
    the full refined intent; candidates are the open pick-list slot's choices."""
    picking = (
        "The proposal is currently asking which of several matches was meant. "
        'The choices, each as "<ref> = label", in order:\n'
        + "\n".join(candidates)
        + '\nIf the correction picks one (by name, part of it, or position like "the first '
        'one"), echo the chosen candidate\'s ref: "id:<n>" is a person — use it as the '
        'ref in your output; "expense_id:<n>" is an expense — set your output\'s '
        '"expense_id" to <n>.\n'
        if candidates
        else ""
    )
    user = (
        f"Current proposed intent:\n{json.dumps(prior_intent)}\n"
        f"{picking}"
        f"Correction reply:\n{correction}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT + REFINE_ADDENDUM},
        {"role": "user", "content": user},
    ]


def retry_messages(
    messages: list[dict[str, str]], bad_reply: str, error: str
) -> list[dict[str, str]]:
    """Show the model its own invalid reply and what was wrong (issue #13 grill)."""
    return [
        *messages,
        {"role": "assistant", "content": bad_reply},
        {
            "role": "user",
            "content": (
                f"That reply wasn't valid: {error}\n"
                "Reply again with ONLY the corrected JSON object."
            ),
        },
    ]
