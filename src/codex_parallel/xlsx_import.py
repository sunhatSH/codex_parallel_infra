"""Import accounts from the provider's xlsx export into the hub token pool.

Expected sheet layout (matches 10个gpt.xlsx):
  col A: 账号----密码   (label source; also useful for ops)
  col G: 购买链接       (ignored)
  col P: full token-bundle JSON (refresh_token/access_token/account_id/...)

Each row becomes one Token: secret = auth.json (built from the bundle;
RT-exchange is attempted only when the bundled access_token is expired).
"""

from __future__ import annotations

import json
from pathlib import Path


def parse_accounts_xlsx(path: str | Path) -> list[tuple[str, dict]]:
    """Return [(label, bundle_json), ...] — labels deduped, secrets NOT printed."""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=True)
    ws = wb.worksheets[0]

    accounts: list[tuple[str, dict]] = []
    seen_labels: set[str] = set()
    seen_accounts: set[str] = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row:
            continue
        label_raw = (row[0] or "") if len(row) > 0 else ""
        bundle_raw = (row[15] or "") if len(row) > 15 else ""
        label = str(label_raw).split("----")[0].strip() or f"acc-{len(accounts):04d}"
        if not str(bundle_raw).strip():
            continue
        try:
            bundle = json.loads(str(bundle_raw))
        except json.JSONDecodeError:
            continue
        rt = bundle.get("refresh_token")
        if not rt:
            continue
        if label in seen_labels:
            label = f"{label}-{len(accounts):04d}"
        seen_labels.add(label)
        accounts.append((label, bundle))
    return accounts


def import_xlsx_to_pool(pool, xlsx_path: str | Path, *, exchange: bool = True) -> dict:
    """Parse xlsx, build auth.json per account, add to the token pool.

    exchange=True: try RT exchange for accounts whose bundled access_token
    is missing/expired (needs network to auth.openai.com). Failures fall
    back to the bundled tokens; accounts with neither path are skipped and
    reported.

    Returns {"added": [...labels], "skipped": [(label, reason)], "stats": {...}}
    """
    from .token_exchange import TokenBundle, TokenExchangeError

    added: list[str] = []
    skipped: list[tuple[str, str]] = []

    for label, bundle in parse_accounts_xlsx(xlsx_path):
        tb = TokenBundle.from_bundle_json(label, bundle)
        try:
            auth_json = tb.build_auth_json() if exchange else tb._from_bundled()
            secret = json.dumps(auth_json, ensure_ascii=False)
            tok = pool.add_token(label, secret, max_concurrency=1)
            added.append(tok.label)
        except TokenExchangeError as e:
            # exchange failed — but the exchange cache may still hold a usable
            # auth.json from a PREVIOUS import of the same RT (re-exchanging
            # the same RT would burn it with refresh_token_reused).
            from .token_exchange import get_exchange_cache

            cached = get_exchange_cache().lookup_usable(tb.refresh_token)
            if cached is not None:
                try:
                    pool.add_token(
                        label, json.dumps(cached, ensure_ascii=False), max_concurrency=1
                    )
                    added.append(label)
                    continue
                except ValueError:
                    pass
            # still nothing: store the bare RT; the runner retries the
            # exchange when a proxy is configured.
            if tb.refresh_token:
                try:
                    pool.add_token(f"{label}", tb.refresh_token, max_concurrency=1)
                    added.append(label)
                    continue
                except ValueError:
                    pass
            skipped.append((label, f"exchange failed: {e}"))
        except ValueError as e:
            skipped.append((label, f"duplicate or invalid: {e}"))

    return {"added": added, "skipped": skipped, "stats": pool.stats()}
