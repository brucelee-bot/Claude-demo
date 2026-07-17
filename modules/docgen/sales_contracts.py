import hashlib
import os
import re


SALES_CONTRACT_YEARS = ("2023", "2024", "2025")


def format_sales_contract_code(year, sequence):
    year_text = str(year or "").strip()
    try:
        sequence_number = int(sequence)
    except (TypeError, ValueError):
        sequence_number = 0
    if year_text not in SALES_CONTRACT_YEARS or sequence_number < 1:
        return ""
    return f"{year_text}合同{sequence_number:02d}"


def parse_sales_contract_sequence(code, year=""):
    match = re.fullmatch(r"(2023|2024|2025)合同(\d+)", str(code or "").strip())
    if not match:
        return 0
    if year and match.group(1) != str(year).strip():
        return 0
    sequence = int(match.group(2))
    return sequence if sequence >= 1 else 0


def _next_available_sequence(used_sequences):
    sequence = 1
    while sequence in used_sequences:
        sequence += 1
    return sequence


def sales_contract_file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _year_from_text(value):
    match = re.search(r"(?<!\d)(2023|2024|2025)(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def infer_sales_contract_year(item, relation_rows=None):
    """Recover a legacy contract year from maintained metadata before row context."""
    if not isinstance(item, dict):
        return ""

    explicit_year = str(item.get("year") or "").strip()
    if explicit_year in SALES_CONTRACT_YEARS:
        return explicit_year

    code_year = _year_from_text(item.get("contract_code"))
    if code_year:
        return code_year

    for key in ("original_filename", "stored_filename", "relative_path"):
        filename_year = _year_from_text(item.get(key))
        if filename_year:
            return filename_year

    file_id = str(item.get("id") or "").strip()
    if file_id:
        for row in relation_rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("sales_contract_file_id") or "").strip() != file_id:
                continue
            row_year = str(row.get("year") or "").strip()
            if row_year in SALES_CONTRACT_YEARS:
                return row_year
    return ""


def _contract_identity_keys(item):
    keys = []
    sha256 = str(item.get("sha256") or "").strip().lower()
    relative_path = os.path.normpath(str(item.get("relative_path") or "").strip())
    file_id = str(item.get("id") or "").strip()
    if sha256:
        keys.append(("sha256", sha256))
    if relative_path and relative_path != ".":
        keys.append(("path", relative_path))
    if file_id:
        keys.append(("id", file_id))
    return keys


def ensure_sales_contract_codes(contracts, relation_rows=None):
    """Normalize legacy contracts and fill stable, per-year sequence numbers."""
    items = [item for item in (contracts or []) if isinstance(item, dict)]
    canonical_by_identity = {}
    canonical_items = []

    for item in items:
        inferred_year = infer_sales_contract_year(item, relation_rows)
        if inferred_year:
            item["year"] = inferred_year

        canonical = next(
            (
                canonical_by_identity[key]
                for key in _contract_identity_keys(item)
                if key in canonical_by_identity
            ),
            None,
        )
        if canonical is None:
            canonical = item
            canonical_items.append(item)
            item.pop("duplicate_of", None)
        else:
            canonical_id = str(canonical.get("id") or "").strip()
            if canonical_id:
                item["duplicate_of"] = canonical_id
            if not canonical.get("year") and item.get("year"):
                canonical["year"] = item["year"]

        for key in _contract_identity_keys(item):
            canonical_by_identity[key] = canonical

    used_by_year = {year: set() for year in SALES_CONTRACT_YEARS}

    for item in canonical_items:
        year = str(item.get("year") or "").strip()
        sequence = parse_sales_contract_sequence(item.get("contract_code"), year)
        if not sequence:
            try:
                candidate = int(item.get("contract_sequence") or 0)
            except (TypeError, ValueError):
                candidate = 0
            sequence = candidate if candidate >= 1 else 0
        if year in used_by_year and sequence and sequence not in used_by_year[year]:
            item["contract_sequence"] = sequence
            item["contract_code"] = format_sales_contract_code(year, sequence)
            used_by_year[year].add(sequence)
        else:
            item.pop("contract_sequence", None)
            item.pop("contract_code", None)

    for item in canonical_items:
        year = str(item.get("year") or "").strip()
        if year not in used_by_year or item.get("contract_code"):
            continue
        sequence = _next_available_sequence(used_by_year[year])
        item["contract_sequence"] = sequence
        item["contract_code"] = format_sales_contract_code(year, sequence)
        used_by_year[year].add(sequence)

    for item in items:
        duplicate_id = str(item.get("duplicate_of") or "").strip()
        if not duplicate_id:
            continue
        canonical = next(
            (
                candidate
                for candidate in canonical_items
                if str(candidate.get("id") or "").strip() == duplicate_id
            ),
            None,
        )
        if not canonical:
            continue
        item["year"] = canonical.get("year", "")
        if canonical.get("contract_sequence"):
            item["contract_sequence"] = canonical["contract_sequence"]
            item["contract_code"] = canonical.get("contract_code", "")
        else:
            item.pop("contract_sequence", None)
            item.pop("contract_code", None)
    return items


def selectable_sales_contracts(contracts, relation_rows=None):
    return [
        item
        for item in ensure_sales_contract_codes(contracts, relation_rows)
        if not item.get("duplicate_of") and item.get("contract_code")
    ]


def remap_sales_contract_rows(relation_rows, contracts):
    """Point legacy rows at canonical contract records and synchronize labels."""
    items = ensure_sales_contract_codes(contracts, relation_rows)
    by_id = {
        str(item.get("id") or "").strip(): item
        for item in items
        if str(item.get("id") or "").strip()
    }
    for row in relation_rows or []:
        if not isinstance(row, dict):
            continue
        file_id = str(row.get("sales_contract_file_id") or "").strip()
        contract = by_id.get(file_id)
        if not contract:
            continue
        duplicate_of = str(contract.get("duplicate_of") or "").strip()
        canonical = by_id.get(duplicate_of) if duplicate_of else contract
        if not canonical or not canonical.get("contract_code"):
            continue
        row["sales_contract_file_id"] = str(canonical.get("id") or "").strip()
        row["sales_contract_code"] = canonical.get("contract_code", "")
        row["sales_contract_filename"] = canonical.get("original_filename", "")
        row["sales_contract_summary"] = (
            canonical.get("summary") or row.get("sales_contract_summary") or ""
        )
        row["sales_contract_keywords"] = (
            canonical.get("keywords") or row.get("sales_contract_keywords") or ""
        )
    return relation_rows


def next_sales_contract_identity(contracts, year):
    year_text = str(year or "").strip()
    if year_text not in SALES_CONTRACT_YEARS:
        return 0, ""
    items = ensure_sales_contract_codes(contracts)
    used = {
        int(item.get("contract_sequence") or 0)
        for item in items
        if str(item.get("year") or "").strip() == year_text
    }
    sequence = _next_available_sequence(used)
    return sequence, format_sales_contract_code(year_text, sequence)
