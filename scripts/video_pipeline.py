"""Versioned local renderer: schema 2 is the landscape newsroom."""
from video_pipeline_legacy import LayoutError

def render_video(episode, audio, runtime_root, work, profile):
    version = episode.get('schema_version', episode.get('schema', 1))
    if str(version).split('.')[0] == '2':
        from video_pipeline_landscape import render_video as render
    elif str(version).split('.')[0] == '1':
        from video_pipeline_legacy import render_video as render
    else:
        raise ValueError(f'Unsupported episode schema: {version!r}')
    return render(episode, audio, runtime_root, work, profile)
