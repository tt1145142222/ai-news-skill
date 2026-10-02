"""Read-only editorial checklists; neither text heuristics nor source rank prove facts."""
import re

ENGLISH = re.compile(r'[A-Za-z]+(?:[-_.][A-Za-z0-9]+)*|\d+(?:[.:/-]\d+)*(?:M|K|%|美元|小时|分钟|秒|年|月|日)?')


def pronunciation_inventory(episode):
    rows = []
    terms = {}
    for scene in episode['scenes']:
        for index, unit in enumerate(scene.get('speech_units', [])):
            display, spoken = unit['display'], unit['text']
            expressions = sorted(set(ENGLISH.findall(display) + ENGLISH.findall(spoken)))
            warnings = []
            if re.search(r'(?<![A-Za-z])AI(?![A-Za-z])', spoken, re.I):
                warnings.append('Unexpanded AI: prefer 人工智能 in Chinese narration; verify actual letters if retained')
            if re.search(r'\b(?:DOT|COLON)\b', spoken, re.I):
                warnings.append('Possible literal punctuation word: inspect intended meaning')
            row_id = f"{scene['id']}/{index}"
            for term in expressions:
                terms.setdefault(term, []).append(row_id)
            rows.append({'id': row_id, 'scene_id': scene['id'], 'unit_index': index, 'display': display,
                         'spoken': spoken, 'expressions': expressions, 'warnings': warnings})
    return {'method': 'editorial checklist only; does not synthesize, alter text, or prove pronunciation',
            'unit_count': len(rows), 'terms': [{'expression': k, 'units': v} for k, v in sorted(terms.items())],
            'units': rows, 'pronunciation_review': 'pending actual audio review'}


QUALIFIERS = re.compile(
    r'至少|至多|超过|不足|约|最高|最低|仅限|似乎|疑似|尚未|未确认|未证实|计划|试点|'
    r'发现时|截至|期间|据.{0,12}称|报告称|官方称|厂商调查|'
    r'\bat least\b|\bat most\b|\bmore than\b|\bup to\b|\bapproximately\b|'
    r'\bappears?\b|\bsuspected\b|\bwe assess\b|\bconfidence\b|\bpilot\b|'
    r'\bcould not confirm\b|\bnot independently\b', re.I)


def factual_inventory(episode):
    """Expose evidence and public uses for review, without guessing semantic entailment."""
    source_map = {s['id']: s for s in episode['sources']}
    rows = []
    for claim in episode['claims']:
        uses = []
        if claim['id'] in episode.get('title_claim_ids', []):
            uses.append({'layer': 'titles-and-description', 'text': '\n'.join(
                episode.get(k, '') for k in ('title', 'title_short', 'description'))})
        for scene in episode['scenes']:
            if claim['id'] not in scene.get('claim_ids', []):
                continue
            uses.append({'layer': scene['id'] + '/headline-speech-captions',
                         'text': '\n'.join(scene['head'] + [scene['speech']] +
                                           [u['display'] for u in scene.get('speech_units', [])])})
            uses.extend({'layer': scene['id'] + f'/card-{i + 1}',
                         'text': card['title'] + '\n' + card['body']}
                        for i, card in enumerate(scene.get('visual', {}).get('cards', []))
                        if claim['id'] in card.get('claim_ids', []))
            uses.extend({'layer': scene['id'] + '/media-' + media['id'], 'asset': media['path'],
                         **{key: media[key] for key in ('context_note', 'caption', 'translation', 'annotation') if key in media}}
                        for media in scene.get('media', []) if claim['id'] in media.get('claim_ids', []))
        evidence = []
        for item in claim['evidence']:
            source = source_map[item['source_id']]
            evidence.append({'source_id': source['id'], 'publisher': source['publisher'], 'url': source['url'],
                             'source_type': source['type'], 'snapshot': source['snapshot'],
                             'published': source.get('published_at') or source['published_date'],
                             'excerpt': item['excerpt'],
                             'qualifiers_to_check': sorted(set(QUALIFIERS.findall(item['excerpt'])))})
        public_text = '\n'.join(u.get('text', '') for u in uses)
        claim_text = '\n'.join([claim['text'], claim.get('limits', '')])
        rows.append({'id': claim['id'], 'claim': claim['text'], 'limits': claim.get('limits', ''),
                     'public_numbers': sorted(set(re.findall(r'\d+(?:[.,:/-]\d+)*', public_text))),
                     'public_qualifiers': sorted(set(QUALIFIERS.findall(public_text))),
                     'claim_numbers': sorted(set(re.findall(r'\d+(?:[.,:/-]\d+)*', claim_text))),
                     'claim_qualifiers': sorted(set(QUALIFIERS.findall(claim_text))),
                     'evidence': evidence, 'public_uses': uses, 'review': 'pending'})
    return {'method': 'Review aid only. Excerpt membership, source authority, quantities and keywords do not establish entailment, independence or underlying attribution. Open every actual media asset; this inventory performs no OCR or visual verification.',
            'editorial_research': episode.get('editorial_research'),
            'checks': ['Read body passages, not just report or section headings; split unsupported compound claims.',
                       'Track lower/upper bounds, denominators, case versus subcase/person/account units and observation periods.',
                       'Separate report publication, activity period, observed state and current status.',
                       'Separate authors assertions from independently corroborated underlying facts; inspect counterevidence and uncertainties.',
                       'For long reports, compare every section to the coverage record; included facts need complete support, omitted material needs a reason.'],
            'claim_count': len(rows), 'claims': rows}
