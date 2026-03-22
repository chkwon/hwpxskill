#!/usr/bin/env python3
"""
cite_numeric.py — Convert author-year citations to numeric style in HWPX files.

Numbers are assigned by order of first appearance in the body text.
Consecutive numbers are compressed: [2-5] instead of [2, 3, 4, 5].
Hybrid mode: [2, 3, 6-9].

Re-runnable: if the document already has [N] numeric citations and new
author-year citations were added, re-running renumbers everything from scratch.

Usage:
    python3 cite_numeric.py input.hwpx --output output.hwpx
    python3 cite_numeric.py input.hwpx --output output.hwpx --dry-run
    python3 cite_numeric.py input.hwpx --output output.hwpx --ref-heading "5. References" --next-heading "6."
"""

import argparse
import re
import sys
import zipfile


# ─── Utilities ───────────────────────────────────────────────────────────────

def format_numbers(nums):
    """Format sorted numbers with range compression.
    [9,10,11,12] -> '9\u201312'   (en-dash for 3+ consecutive)
    [7,8]        -> '7, 8'       (comma for 2 consecutive)
    [2,3,6,7,8,9]-> '2, 3, 6\u20139'
    """
    if not nums:
        return ""
    nums = sorted(set(nums))
    ranges = []
    start = end = nums[0]
    for n in nums[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append((start, end))
            start = end = n
    ranges.append((start, end))
    parts = []
    for s, e in ranges:
        if s == e:
            parts.append(str(s))
        elif e == s + 1:
            parts.append(f"{s}, {e}")
        else:
            parts.append(f"{s}\u2013{e}")
    return ", ".join(parts)


def extract_citation_key_from_ref(ref_text):
    """Extract a normalized citation key from a reference entry.
    'Bengio, Y., Lodi, A., & Prouvost, A. (2021). Machine learning...'
    -> 'Bengio et al., 2021'
    'Fisher, M. L. (1981). The Lagrangian relaxation...'
    -> 'Fisher, 1981'
    """
    ref_text = ref_text.replace('&amp;amp;', '&').replace('&amp;', '&')
    # Remove [N] prefix if present
    ref_text = re.sub(r'^\[\d+\]\s*', '', ref_text)

    # Extract year
    year_match = re.search(r'\((\d{4})\)', ref_text)
    if not year_match:
        return None
    year = year_match.group(1)

    # Extract authors (everything before the year parenthetical)
    authors_part = ref_text[:year_match.start()].strip().rstrip('.')

    # Count authors by splitting on ', ' and '&'
    # Heuristic: split by ' & ' first, then by commas
    # Each author has "Surname, Initial." pattern
    # Count how many surname-initial pairs exist

    # Special cases
    if authors_part.startswith('U.S.'):
        return f"U.S. DOE, {year}"

    # Split by ' & ' to separate last author
    ampersand_parts = re.split(r'\s*&\s*', authors_part)

    if len(ampersand_parts) == 1:
        # Single author or "et al." or multiple authors without &
        comma_parts = [p.strip() for p in authors_part.split(',') if p.strip()]
        surname = comma_parts[0].strip()
        # Each author takes ~2 comma-separated items (surname, initials)
        # "Surname1, I., Surname2, I." has 4+ non-empty comma parts = 2+ authors
        if len(comma_parts) > 3:
            return f"{surname} et al., {year}"
        elif len(comma_parts) > 2:
            # Could be "Surname, I., et al." or 2 authors
            return f"{surname} et al., {year}"
        else:
            return f"{surname}, {year}"
    elif len(ampersand_parts) == 2:
        # "A, I. & B, I." (2 authors) or "A, I., B, I., & C, I." (3+ authors)
        first_part = ampersand_parts[0].strip().rstrip(',').strip()
        comma_parts = [p.strip() for p in first_part.split(',') if p.strip()]
        first_surname = comma_parts[0].strip()

        # Count author-initial pairs: each author = surname + initials = ~2 comma items
        # If >2 non-empty items, there are multiple authors before &
        if len(comma_parts) > 3:
            # 3+ authors before & — try to extract second surname too
            # for citations like "Lee, Jung, et al., 2026"
            # comma_parts: ['Lee', 'J.H.', 'Jung', 'J.', 'Dalmeijer', 'K.']
            second_surname = comma_parts[2].strip() if len(comma_parts) > 2 else None
            if second_surname and second_surname[0].isupper() and len(second_surname) > 1:
                return f"{first_surname}, {second_surname}, et al., {year}"
            return f"{first_surname} et al., {year}"
        else:
            # Exactly 1 author before & (surname, initials = 2 items)
            second_part = ampersand_parts[1].strip()
            second_surname = second_part.split(',')[0].strip()
            if second_surname.startswith('da ') or second_surname.startswith('Da '):
                second_surname = second_part.split(',')[0].strip()
            return f"{first_surname} & {second_surname}, {year}"
    else:
        surname = ampersand_parts[0].split(',')[0].strip()
        return f"{surname} et al., {year}"


def find_citation_groups(xml, body_end_idx):
    """Find all (Author, YYYY) and (A; B; C) citation groups in the body XML.
    Returns list of (start_pos, end_pos, full_match_str, list_of_individual_keys).
    Handles &amp; in XML.
    """
    results = []
    i = 0
    body = xml[:body_end_idx]

    while i < len(body):
        # Find next '('
        paren_start = body.find('(', i)
        if paren_start == -1:
            break
        paren_end = body.find(')', paren_start)
        if paren_end == -1:
            break

        content = body[paren_start + 1:paren_end]
        i = paren_end + 1

        # Skip if inside XML tags
        if '<' in content or '>' in content:
            # But allow &amp; which is legitimate in citations
            cleaned = re.sub(r'&amp;', '&', content)
            if '<' in cleaned or '>' in cleaned:
                continue
            # Restore for further processing
            pass

        # Skip date ranges, numbered items, etc.
        if '~' in content:
            continue

        # Check if this looks like a citation (contains year)
        if not re.search(r'\d{4}', content):
            continue

        # Check if it contains author-like text
        content_decoded = content.replace('&amp;amp;', '&').replace('&amp;', '&')
        if not re.search(r'[A-Za-z]{2,}', content_decoded):
            continue

        # Skip things like (1), (FTQC), (MILP), (2021, 2024), etc.
        if re.match(r'^\d', content_decoded) and ',' not in content_decoded:
            continue
        # Skip parenthetical comments that happen to contain a year
        if 'signature' in content_decoded.lower():
            continue

        # Split by ; for multi-citations
        parts = re.split(r'\s*;\s*', content_decoded)
        keys = []
        all_valid = True
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # Must have a year
            if not re.search(r'\d{4}', part):
                all_valid = False
                break
            # Must have author text
            if not re.search(r'[A-Za-z]{2,}', part):
                all_valid = False
                break
            keys.append(part)

        if all_valid and keys:
            full_match = body[paren_start:paren_end + 1]
            results.append((paren_start, paren_end + 1, full_match, keys))

    return results


def find_numeric_citations(xml, body_end_idx):
    """Find existing [N] or [N-M] or [N, M] numeric citations in body.
    Returns list of (start_pos, end_pos, full_match_str, list_of_numbers).
    """
    results = []
    body = xml[:body_end_idx]
    for m in re.finditer(r'\[(\d[\d,\s\u2013\u2014-]*)\]', body):
        full = m.group(0)
        inner = m.group(1)
        # Parse numbers
        nums = []
        for part in re.split(r'[,\s]+', inner):
            part = part.strip()
            if not part:
                continue
            range_match = re.match(r'(\d+)[\u2013\u2014-](\d+)', part)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                nums.extend(range(start, end + 1))
            elif part.isdigit():
                nums.append(int(part))
        if nums:
            results.append((m.start(), m.end(), full, nums))
    return results


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Convert author-year citations to numeric style in HWPX')
    parser.add_argument('input', help='Input HWPX file')
    parser.add_argument('--output', '-o', required=True, help='Output HWPX file')
    parser.add_argument('--ref-heading', default='References', help='References section heading text (default: "References")')
    parser.add_argument('--next-heading', default='6.', help='Next section heading prefix after references (default: "6.")')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without writing')
    args = parser.parse_args()

    # ── Read HWPX ──
    with zipfile.ZipFile(args.input, 'r') as zin:
        all_entries = zin.infolist()
        section_files = sorted([e.filename for e in all_entries
                                if e.filename.startswith('Contents/section') and e.filename.endswith('.xml')])
        section_data = {}
        for sf in section_files:
            section_data[sf] = zin.read(sf).decode('utf-8')

    print(f"Section files: {section_files}")

    # ── Find which section has the references ──
    ref_section_file = None
    ref_heading_pattern = args.ref_heading
    for sf, data in section_data.items():
        if f'<hp:t>' in data:
            # Search for references heading
            idx = data.rfind(ref_heading_pattern)
            if idx != -1:
                ref_section_file = sf
                break

    if ref_section_file is None:
        print(f"ERROR: Could not find '{ref_heading_pattern}' in any section file")
        sys.exit(1)

    print(f"References found in: {ref_section_file}")
    xml = section_data[ref_section_file]

    # ── Find references section boundaries ──
    ref_heading_idx = xml.rfind(f'<hp:t>')
    # More precise: find the actual heading
    search_pattern = ref_heading_pattern
    ref_text_idx = xml.rfind(search_pattern)
    ref_heading_t_start = xml.rfind('<hp:t>', 0, ref_text_idx)
    ref_heading_p_start = xml.rfind('<hp:p ', 0, ref_heading_t_start)
    ref_heading_p_end = xml.find('</hp:p>', ref_text_idx) + len('</hp:p>')

    # Find next section
    next_heading = args.next_heading
    ref_end_idx = len(xml)
    for marker in [f'<hp:t>{next_heading}', f'<hp:t> {next_heading}']:
        idx = xml.find(marker, ref_heading_p_end)
        if idx != -1:
            p_start = xml.rfind('<hp:p ', 0, idx)
            if p_start < ref_end_idx:
                ref_end_idx = p_start

    body_end_idx = ref_heading_p_start  # everything before refs is "body"

    # ── Step 1: Parse existing references ──
    ref_block = xml[ref_heading_p_end:ref_end_idx]
    ref_paras = []
    for m in re.finditer(r'<hp:p [^>]*>.*?</hp:p>', ref_block):
        para_xml = m.group()
        text_match = re.search(r'<hp:t>(.*?)</hp:t>', para_xml)
        if text_match:
            ref_text = text_match.group(1)
            if len(ref_text) > 20:
                ref_paras.append((ref_text, para_xml))

    print(f"Reference entries found: {len(ref_paras)}")

    # ── Step 2: Build citation key -> reference mapping ──
    ref_by_key = {}  # citation_key -> (ref_text, para_xml)
    for ref_text, para_xml in ref_paras:
        key = extract_citation_key_from_ref(ref_text)
        if key:
            # Handle &amp; -> & for matching
            ref_by_key[key] = (ref_text, para_xml)
        else:
            print(f"  WARNING: Could not extract key from: {ref_text[:80]}")

    print(f"Citation keys extracted: {len(ref_by_key)}")

    # ── Step 3: Check for existing numeric citations (re-run scenario) ──
    existing_numeric = find_numeric_citations(xml, body_end_idx)
    if existing_numeric:
        print(f"\nFound {len(existing_numeric)} existing numeric citations — reverting to author-year first")
        # Build reverse map: number -> citation key from reference entries
        num_to_key = {}
        for ref_text, _ in ref_paras:
            # Check for [N] prefix
            prefix_match = re.match(r'\[(\d+)\]\s*(.*)', ref_text)
            if prefix_match:
                num = int(prefix_match.group(1))
                remaining = prefix_match.group(2)
                key = extract_citation_key_from_ref(remaining)
                if key:
                    num_to_key[num] = key

        if num_to_key:
            # Revert [N] citations to (Author, Year) — process longest matches first
            numeric_groups = find_numeric_citations(xml, body_end_idx)
            for _, _, full_match, nums in sorted(numeric_groups, key=lambda x: len(x[2]), reverse=True):
                if len(nums) == 1 and nums[0] in num_to_key:
                    key = num_to_key[nums[0]]
                    xml_key = key.replace('&', '&amp;')
                    xml = xml.replace(full_match, f'({xml_key})')
                elif all(n in num_to_key for n in nums):
                    keys = [num_to_key[n] for n in nums]
                    xml_keys = [k.replace('&', '&amp;') for k in keys]
                    xml = xml.replace(full_match, '(' + '; '.join(xml_keys) + ')')

            # Also remove [N] prefixes from references
            for ref_text, para_xml in ref_paras:
                cleaned = re.sub(r'^\[\d+\]\s*', '', ref_text)
                if cleaned != ref_text:
                    xml = xml.replace(f'<hp:t>{ref_text}</hp:t>', f'<hp:t>{cleaned}</hp:t>')

            # Re-parse after reversion
            ref_heading_p_end_new = xml.find('</hp:p>', xml.rfind(ref_heading_pattern)) + len('</hp:p>')
            ref_end_idx_new = len(xml)
            for marker in [f'<hp:t>{next_heading}', f'<hp:t> {next_heading}']:
                idx = xml.find(marker, ref_heading_p_end_new)
                if idx != -1:
                    p_start = xml.rfind('<hp:p ', 0, idx)
                    if p_start < ref_end_idx_new:
                        ref_end_idx_new = p_start

            ref_heading_p_end = ref_heading_p_end_new
            ref_end_idx = ref_end_idx_new
            body_end_idx = xml.rfind('<hp:p ', 0, xml.rfind(ref_heading_pattern))

            # Re-parse references
            ref_block = xml[ref_heading_p_end:ref_end_idx]
            ref_paras = []
            for m in re.finditer(r'<hp:p [^>]*>.*?</hp:p>', ref_block):
                para_xml = m.group()
                text_match = re.search(r'<hp:t>(.*?)</hp:t>', para_xml)
                if text_match:
                    ref_text = text_match.group(1)
                    if len(ref_text) > 20:
                        ref_paras.append((ref_text, para_xml))

            ref_by_key = {}
            for ref_text, para_xml in ref_paras:
                key = extract_citation_key_from_ref(ref_text)
                if key:
                    ref_by_key[key] = (ref_text, para_xml)

            print(f"After reversion: {len(ref_paras)} references, {len(ref_by_key)} keys")

    # ── Step 4: Scan body for citation groups and assign numbers ──
    citation_groups = find_citation_groups(xml, body_end_idx)
    print(f"\nCitation groups found in body: {len(citation_groups)}")

    # Assign numbers in order of first appearance
    key_to_num = {}
    next_num = 1
    ordered_groups = sorted(citation_groups, key=lambda x: x[0])  # by position

    for pos, end, full_match, keys in ordered_groups:
        for key in keys:
            if key not in key_to_num:
                key_to_num[key] = next_num
                next_num += 1

    print(f"Unique citations: {len(key_to_num)}")
    for key, num in sorted(key_to_num.items(), key=lambda x: x[1]):
        print(f"  [{num:2d}] {key}")

    # ── Step 5: Replace in-text citations ──
    # Build replacement pairs
    replacements = []
    seen_matches = set()
    for pos, end, full_match, keys in citation_groups:
        if full_match in seen_matches:
            continue
        seen_matches.add(full_match)
        nums = [key_to_num[k] for k in keys if k in key_to_num]
        if nums:
            formatted = format_numbers(nums)
            replacement = f"[{formatted}]"
            # Also build the XML version of the match (with &amp;)
            xml_match = full_match
            replacements.append((xml_match, replacement))

    # Sort by length (longest first) to avoid partial matches
    replacements.sort(key=lambda x: len(x[0]), reverse=True)

    if args.dry_run:
        print("\n=== DRY RUN — replacements that would be made ===")
        for old, new in replacements:
            print(f"  {old[:65]:65s} -> {new}")
        print(f"\nTotal: {len(replacements)} replacements")
        return

    for old, new in replacements:
        count = xml.count(old)
        if count > 0:
            xml = xml.replace(old, new)
            print(f"  {old[:60]:60s} -> {new:12s} ({count}x)")

    # Check for remaining unconverted citations
    remaining = re.findall(
        r'\([^()]*?(?:et al\.|[A-Z][a-z]{2,})[^()]*?\d{4}[^()]*?\)',
        xml[:body_end_idx]
    )
    remaining = [r for r in remaining if '~' not in r and 'FTQC' not in r
                 and 'signature' not in r.lower() and not r.startswith('(0')
                 and 'DOE' not in r]
    if remaining:
        print(f"\nWARNING: {len(remaining)} citations may be unconverted:")
        for r in remaining:
            print(f"  {r}")

    # ── Step 6: Remove all linesegarray blocks ──
    lineseg_count = len(re.findall(r'<hp:linesegarray>.*?</hp:linesegarray>', xml))
    xml = re.sub(r'<hp:linesegarray>.*?</hp:linesegarray>', '', xml)
    print(f"\nRemoved {lineseg_count} linesegarray blocks")

    # ── Step 7: Reorder and number references ──
    # Re-find boundaries after replacements
    ref_text_idx = xml.rfind(ref_heading_pattern)
    ref_heading_p_end = xml.find('</hp:p>', ref_text_idx) + len('</hp:p>')
    ref_end_idx = len(xml)
    for marker in [f'<hp:t>{next_heading}', f'<hp:t> {next_heading}']:
        idx = xml.find(marker, ref_heading_p_end)
        if idx != -1:
            p_start = xml.rfind('<hp:p ', 0, idx)
            if p_start < ref_end_idx:
                ref_end_idx = p_start

    ref_block = xml[ref_heading_p_end:ref_end_idx]
    ref_paras = []
    for m in re.finditer(r'<hp:p [^>]*>.*?</hp:p>', ref_block):
        para_xml = m.group()
        text_match = re.search(r'<hp:t>(.*?)</hp:t>', para_xml)
        if text_match:
            ref_text = text_match.group(1)
            if len(ref_text) > 20:
                ref_paras.append((ref_text, para_xml))

    # Build a normalized lookup for flexible matching
    def normalize_key(k):
        """Normalize a citation key for matching."""
        return k.replace('&amp;amp;', '&').replace('&amp;', '&').strip()

    norm_key_to_num = {normalize_key(k): v for k, v in key_to_num.items()}

    # Also build a fuzzy lookup: (first_surname, year) -> num
    # This handles cases where citation uses "Lee, Jung, et al., 2026"
    # but ref extraction produces "Lee et al., 2026"
    # IMPORTANT: skip ambiguous keys where multiple citations share (surname, year)
    fuzzy_key_to_num = {}
    fuzzy_key_ambiguous = set()
    for k, v in key_to_num.items():
        nk = normalize_key(k)
        parts = nk.rsplit(',', 1)
        if len(parts) == 2:
            year_part = parts[1].strip()
            surname_part = parts[0].split(',')[0].split(' ')[0].strip()
            fuzzy_key = (surname_part, year_part)
            if fuzzy_key in fuzzy_key_to_num:
                fuzzy_key_ambiguous.add(fuzzy_key)
            else:
                fuzzy_key_to_num[fuzzy_key] = v
    # Remove ambiguous keys
    for fk in fuzzy_key_ambiguous:
        del fuzzy_key_to_num[fk]
        print(f"  WARNING: Ambiguous fuzzy key {fk} — skipping fuzzy match for this")

    # Match references to citation numbers
    numbered_refs = []
    unmatched_refs = []
    for ref_text, para_xml in ref_paras:
        key = extract_citation_key_from_ref(ref_text)
        matched = False
        if key:
            nk = normalize_key(key)
            if nk in norm_key_to_num:
                numbered_refs.append((norm_key_to_num[nk], ref_text, para_xml))
                matched = True
            else:
                # Fuzzy match: first surname + year
                parts = nk.rsplit(',', 1)
                if len(parts) == 2:
                    surname = parts[0].split(',')[0].split(' ')[0].strip()
                    yr = parts[1].strip()
                    fk = (surname, yr)
                    if fk in fuzzy_key_to_num:
                        numbered_refs.append((fuzzy_key_to_num[fk], ref_text, para_xml))
                        matched = True
        if not matched:
            unmatched_refs.append(ref_text[:80])

    numbered_refs.sort(key=lambda x: x[0])

    print(f"\nReferences matched: {len(numbered_refs)}, unmatched: {len(unmatched_refs)}")
    if unmatched_refs:
        print("Unmatched (will be dropped):")
        for r in unmatched_refs:
            print(f"  - {r}")

    # Rebuild reference block
    new_ref_block = ''
    for num, ref_text, orig_para_xml in numbered_refs:
        # Remove existing [N] prefix if present
        clean_text = re.sub(r'^\[\d+\]\s*', '', ref_text)
        new_text = f'[{num}] {clean_text}'
        new_para = re.sub(
            r'<hp:t>.*?</hp:t>',
            f'<hp:t>{new_text}</hp:t>',
            orig_para_xml,
            count=1
        )
        new_para = re.sub(r'<hp:linesegarray>.*?</hp:linesegarray>', '', new_para)
        new_ref_block += new_para

    xml = xml[:ref_heading_p_end] + new_ref_block + xml[ref_end_idx:]
    print(f"References reordered: [1]-[{max(n for n,_,_ in numbered_refs) if numbered_refs else 0}]")

    # ── Step 8: Write output ──
    modified_bytes = xml.encode('utf-8')
    with zipfile.ZipFile(args.input, 'r') as zin:
        with zipfile.ZipFile(args.output, 'w') as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == ref_section_file:
                    data = modified_bytes
                zout.writestr(item, data)

    print(f"\nWritten: {args.output}")


if __name__ == '__main__':
    main()
