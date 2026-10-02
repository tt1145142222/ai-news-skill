"""Resolve one frozen news interval from a verified local delivery, never latest history."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import re

POLICY = 'since-previous-or-24h'
TZ = timezone(timedelta(hours=8))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    require(isinstance(value, str), 'News window timestamp must be an ISO string')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'News window timestamp requires an explicit timezone')
    return result


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    require(path.is_file(), 'Missing previous delivery anchor file: ' + str(path))
    return json.loads(path.read_text(encoding='utf-8-sig'))


def enabled(profile):
    policy = (profile or {}).get('editorial', {}).get('window_policy')
    require(policy in (None, POLICY), 'Unknown editorial news window policy')
    return policy == POLICY


@dataclass(frozen=True)
class NewsWindow:
    start: datetime
    end: datetime
    start_inclusive: bool
    mode: str
    previous_cutoff: datetime | None = None
    previous_delivery: Path | None = None
    fallback_reason: str | None = None
    anchors: tuple = ()
    policy: str | None = None

    def contains(self, value):
        lower = self.start <= value if self.start_inclusive else self.start < value
        return lower and value <= self.end

    def record(self):
        return {
            'policy': self.policy, 'mode': self.mode,
            'start': self.start.astimezone(TZ).isoformat(),
            'end': self.end.astimezone(TZ).isoformat(),
            'start_inclusive': self.start_inclusive,
            'previous_cutoff': self.previous_cutoff.astimezone(TZ).isoformat() if self.previous_cutoff else None,
            'previous_delivery': str(self.previous_delivery) if self.previous_delivery else None,
            'fallback_reason': self.fallback_reason,
        }


def resolve_window(ep, profile=None, path=None):
    """Validate the selected predecessor identity without rechecking its old toolchain or MP4."""
    cutoff = timestamp(ep['cutoff'])
    if not enabled(profile):
        return NewsWindow(cutoff - timedelta(hours=24), cutoff, True, 'legacy-24h')
    selection = ep.get('news_window')
    require(isinstance(selection, dict) and 'previous_delivery' in selection,
            'Set news_window.previous_delivery to a verified predecessor delivery.json or explicit null with fallback_reason')
    require(set(selection) <= {'previous_delivery', 'fallback_reason'},
            'news_window accepts only previous_delivery and fallback_reason; interval boundaries are computed')
    previous = selection['previous_delivery']
    if previous is None:
        reason = selection.get('fallback_reason')
        require(isinstance(reason, str) and bool(reason.strip()),
                'Record why no reliable previous delivery is available in news_window.fallback_reason')
        return NewsWindow(cutoff - timedelta(hours=24), cutoff, True, 'no-previous-24h',
                          fallback_reason=reason.strip(), policy=POLICY)
    require(isinstance(previous, str) and bool(previous.strip()),
            'news_window.previous_delivery must be a delivery.json path or explicit null')
    require(not selection.get('fallback_reason'), 'fallback_reason is only for a missing predecessor')
    delivery = Path(previous)
    require(delivery.is_absolute() or path is not None,
            'Relative previous_delivery needs the episode file path')
    if not delivery.is_absolute():
        delivery = Path(path).resolve().parent / delivery
    delivery = delivery.resolve()
    require(delivery.name == 'delivery.json', 'Previous delivery must point to work/delivery.json')
    parent = delivery.parent
    manifest_path, review_path, episode_path = (parent / name for name in
                                               ('manifest.json', 'review.json', 'episode.json'))
    receipt, manifest, review, previous_ep = (read(p) for p in
                                             (delivery, manifest_path, review_path, episode_path))
    require(receipt.get('manifest_sha256') == digest(manifest_path), 'Previous delivery manifest hash mismatch')
    require(receipt.get('review_sha256') == digest(review_path), 'Previous delivery review hash mismatch')
    require(review.get('manifest_sha256') == digest(manifest_path), 'Previous delivery review is stale')
    require(review.get('status') == 'passed', 'Previous delivery review has not passed')
    require(manifest.get('fixture') is False and previous_ep.get('fixture', False) is False,
            'Historical fixtures cannot be previous production deliveries')
    tracked = manifest.get('tracked', {})
    recorded_episode = [value for name, value in tracked.items()
                        if Path(name).resolve() == episode_path]
    require(recorded_episode == [digest(episode_path)], 'Previous delivered episode is missing or changed in its manifest')
    require(receipt.get('date') == manifest.get('episode_date') == previous_ep.get('date'),
            'Previous delivery date identity mismatch')
    require(isinstance(previous_ep.get('title'), str) and bool(previous_ep['title'].strip())
            and receipt.get('title') == previous_ep['title'], 'Previous delivery title identity mismatch')
    video = receipt.get('video')
    require(isinstance(video, str) and bool(video.strip())
            and re.fullmatch(r'[0-9a-f]{64}', receipt.get('sha256', {}).get(Path(video).name, '')),
            'Previous delivery requires its completed video receipt hash')
    previous_cutoff = timestamp(previous_ep['cutoff'])
    require(previous_ep['date'] == previous_cutoff.astimezone(TZ).date().isoformat(),
            'Previous episode date differs from its Beijing cutoff date')
    require(previous_cutoff < cutoff,
            'Previous cutoff must precede this cutoff; same-episode revisions must retain their original predecessor')
    anchors = (delivery, manifest_path, review_path, episode_path)
    gap = cutoff - previous_cutoff
    if gap < timedelta(hours=72):
        return NewsWindow(previous_cutoff, cutoff, False, 'since-previous', previous_cutoff,
                          delivery, anchors=anchors, policy=POLICY)
    return NewsWindow(cutoff - timedelta(hours=24), cutoff, True, 'gap-at-least-72h-24h',
                      previous_cutoff, delivery, anchors=anchors, policy=POLICY)


def window_copy(window):
    start, end = window.start.astimezone(TZ).isoformat(), window.end.astimezone(TZ).isoformat()
    if window.mode == 'since-previous':
        reason = '距上期不足72小时，覆盖上期采编截止之后至本期截止，起点不含、终点含'
    elif window.mode == 'gap-at-least-72h-24h':
        reason = '距上期已达72小时，按本期采编截止前24小时收录，含起止时刻'
    elif window.mode == 'no-previous-24h':
        reason = '无可靠上期记录，按本期采编截止前24小时收录，含起止时刻'
    else:
        reason = '生成时刻前24小时'
    return f'本期新闻范围：{start} 至 {end}（{reason}）。\n\n'
