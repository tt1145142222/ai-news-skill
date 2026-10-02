"""Event-scoped editorial gates. Structural checks are not factual or authorization proof."""
from datetime import date
from pathlib import Path
import re


CONTRACT = 'event-scoped-v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def enabled(profile):
    value = (profile or {}).get('editorial', {}).get('evidence_contract')
    require(value in (None, CONTRACT), 'Unknown editorial evidence contract')
    return value == CONTRACT


def text_record(value, base, label):
    require(isinstance(value, str) and value.strip(), 'Missing ' + label)
    path = (Path(base).parent / value).resolve()
    require(path.is_file(), 'Missing ' + label + ': ' + str(path))
    text = path.read_text(encoding='utf-8-sig')
    require(len(text.strip()) >= 80, label + ' must contain an actual record')
    return path, text


def has_date(text, value):
    day = date.fromisoformat(value)
    return any(token in text for token in (
        day.isoformat(), f'{day.year}年{day.month}月{day.day}日',
        f'{day.year}年{day.month:02}月{day.day:02}日'))


def validate_retrospectives(ep, path):
    """Bind each exception to one event and a frozen user-request record, never a brand."""
    rows = ep.get('retrospectives', [])
    require(isinstance(rows, list), 'retrospectives must be a list')
    sources = {s['id']: s for s in ep['sources']}
    records, result, events = [], {}, set()
    for row in rows:
        rid = row.get('id', '')
        require(isinstance(rid, str) and re.fullmatch(r'[A-Za-z0-9_-]+', rid), 'Invalid retrospective id')
        require(rid not in result, 'Duplicate retrospective id: ' + rid)
        event = row.get('event_key', '')
        require(isinstance(event, str) and event.strip() and event not in events, 'One retrospective authorization per event')
        events.add(event)
        published = row.get('publication_date', '')
        date.fromisoformat(published)
        allowed = row.get('source_ids', [])
        require(isinstance(allowed, list) and allowed and len(allowed) == len(set(allowed))
                and set(allowed) <= set(sources), 'Retrospective needs explicit known source_ids: ' + rid)
        require(all(sources[sid].get('context_only') is True for sid in allowed),
                'Retrospective sources must retain context_only: ' + rid)
        require(any(sources[sid]['published_date'] == published for sid in allowed),
                'Retrospective publication date must match an authorized source: ' + rid)
        auth = row.get('authorization', {})
        quote = auth.get('quote', '')
        require(isinstance(quote, str) and len(quote.strip()) >= 4, 'Missing actual user request for retrospective: ' + rid)
        record, content = text_record(auth.get('record'), path, 'retrospective authorization record')
        require(quote in content and event in content and published in content
                and all(sources[sid]['url'] in content for sid in allowed),
                'Authorization record must bind the exact user request, event, original date and source URLs: ' + rid)
        records.append(record)
        for public in [ep.get('description', ''), ep['scenes'][0].get('speech', '')]:
            require('专题回顾' in public and has_date(public, published),
                    'Disclose each retrospective and its original full date in overview and description: ' + rid)
        result[rid] = row
    used = set()
    for scene in ep['scenes']:
        editorial = scene.get('editorial', {})
        rid = editorial.get('retrospective_id')
        if rid is None:
            continue
        require(scene.get('layout') == 'news' and rid in result, 'Unknown retrospective on news scene: ' + scene['id'])
        row = result[rid]
        require(editorial.get('event_key') == row['event_key'], 'Retrospective event mismatch: ' + scene['id'])
        require(set(scene.get('source_ids', [])) <= set(row['source_ids']),
                'Retrospective scene includes sources outside the authorized event: ' + scene['id'])
        require('专题回顾' in scene.get('source_label', '') and has_date(scene['source_label'], row['publication_date']),
                'Show 专题回顾 and original full date on every retrospective scene: ' + scene['id'])
        used.add(rid)
    require(used == set(result), 'Every retrospective authorization must be used by its event')
    return result, records


def validate_editorial_input(ep, path, profile):
    """Return real files to hash; no invented sources, semantic pass, or public posting."""
    retrospectives, records = validate_retrospectives(ep, path)
    if not enabled(profile):
        return retrospectives, records
    record, _ = text_record(ep.get('editorial_research'), path, 'editorial research record')
    records.append(record)
    for scene in ep['scenes']:
        if scene.get('layout') != 'news':
            continue
        editorial = scene.get('editorial', {})
        core = editorial.get('core_claim_ids', [])
        require(isinstance(core, list) and core and len(core) == len(set(core))
                and set(core) <= set(scene['claim_ids']),
                'Bind editorial.core_claim_ids to the actual core change: ' + scene['id'])
        require(editorial.get('evidence_mode', 'direct') in ('direct', 'attributed', 'investigation'),
                'Unknown editorial.evidence_mode: ' + scene['id'])
        if editorial.get('retrospective_id'):
            require(editorial.get('evidence_mode') in ('attributed', 'investigation'),
                    'Retrospective requires attributed evidence review: ' + scene['id'])
    return retrospectives, records


def high_risk_scene_ids(ep):
    return [s['id'] for s in ep['scenes'] if s.get('layout') == 'news'
            and (s.get('editorial', {}).get('evidence_mode') in ('attributed', 'investigation')
                 or s.get('editorial', {}).get('retrospective_id'))]


def review_template(ep):
    result = {'factual_inventory_reviewed': False, 'editorial_research_notes': '',
              'high_risk_evidence_notes': {sid: '' for sid in high_risk_scene_ids(ep)}}
    if ep.get('retrospectives'):
        result['retrospective_authorizations_verified'] = False
    return result


def validate_editorial_review(ep, review):
    require(review.get('factual_inventory_reviewed') is True, 'Read the factual inventory against complete source passages and all public layers')
    notes = review.get('editorial_research_notes', '')
    require(isinstance(notes, str) and len(notes.strip()) >= 20,
            'Record actual discovery coverage, exclusions, unresolved access and core-newness checks')
    risk = review.get('high_risk_evidence_notes', {})
    require(isinstance(risk, dict) and set(risk) == set(high_risk_scene_ids(ep)), 'Review every attributed/investigation scene')
    require(all(isinstance(v, str) and len(v.strip()) >= 30 for v in risk.values()),
            'Record attribution, independent-evidence limits, quantities/observation periods and report coverage for each high-risk scene')
    if ep.get('retrospectives'):
        require(review.get('retrospective_authorizations_verified') is True,
                'Verify event-specific user authorization against the conversation; a quoted file is not authorization by itself')
