"""Local production entrypoint. Search and factual judgment remain agent responsibilities."""
from pathlib import Path
from datetime import datetime, date, timezone, timedelta
import argparse, hashlib, json, os, shutil, sys, tempfile, zipfile, re
sys.dont_write_bytecode=True

SKILL=Path(__file__).resolve().parents[1]
TZ=timezone(timedelta(hours=8))

def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,x):Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')
def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def require(test,msg):
    if not test:raise ValueError(msg)
def iso(x):
    t=datetime.fromisoformat(x.replace('Z','+00:00'))
    require(t.tzinfo is not None,'Use explicit timezone in timestamp: '+x)
    return t
def safe_ids(rows,label):
    ids=[s['id'] for s in rows]
    require(len(ids)==len(set(ids)),f'Duplicate {label} id')
    require(all(re.fullmatch(r'[A-Za-z0-9_-]+',s) for s in ids),f'Invalid {label} id')
    return set(ids)

def validate_news_window(ep,require_core=False,retrospectives=None,profile=None,path=None,window=None):
    """Check the frozen selected interval; source truth/newness still needs cold review."""
    from news_window import resolve_window
    window=window or resolve_window(ep,profile,path)
    cutoff=window.end
    interval='strict 24-hour window' if window.mode=='legacy-24h' else 'computed news window'
    recent=set()
    for source in ep['sources']:
        sid=source['id'];published_at=source.get('published_at')
        published=date.fromisoformat(source['published_date'])
        if published_at:
            moment=iso(published_at)
            require(published==moment.date(),'published_date must match published_at in its recorded timezone: '+sid)
            require(moment<=cutoff,'Source published after cutoff: '+sid)
        else:
            require(published<=cutoff.astimezone(TZ).date(),'Future source publication date: '+sid)
        if source.get('context_only') is True:
            continue
        require(bool(published_at),'News source requires timezone-aware published_at: '+sid)
        require(window.contains(moment),'News source outside the '+interval+': '+sid)
        recent.add(sid)
    claims={claim['id']:claim for claim in ep['claims']}
    news_scenes=[scene for scene in ep['scenes'] if scene.get('layout') not in ('cover','overview','closing')]
    require(bool(news_scenes),'Need at least one news scene with evidence in the '+interval)
    for scene in news_scenes:
        evidence_sources={e['source_id'] for cid in scene.get('claim_ids',[])
                          for e in claims[cid]['evidence']}
        editorial=scene.get('editorial',{})
        rid=editorial.get('retrospective_id')
        if rid is not None:
            require(retrospectives is not None and rid in retrospectives,'Retrospective authorization must be validated first: '+scene['id'])
            allowed=set(retrospectives[rid]['source_ids'])
            require(bool(evidence_sources) and evidence_sources<=allowed,'Retrospective evidence outside authorized event: '+scene['id'])
        elif require_core:
            core=editorial.get('core_claim_ids',[])
            require(bool(core),'Bind core claims to the actual new development: '+scene['id'])
            for cid in core:
                require(cid in scene.get('claim_ids',[]),'Core claim must belong to its scene: '+scene['id'])
                core_sources={e['source_id'] for e in claims[cid]['evidence']}
                require(bool(core_sources & recent),'Each core-change claim needs an in-window non-background source; unrelated recent scene evidence cannot refresh it: '+scene['id']+'/'+cid)
        else:
            require(bool(evidence_sources & recent),
                    'Each news scene needs claim evidence from a non-background source in the '+interval+': '+scene['id'])

def validate(ep,path,fixture=False,profile=None):
    from news_window import enabled as continuity_enabled,resolve_window
    window=resolve_window(ep,profile,path)
    version=ep.get('schema_version')
    require(version in (1,2),'schema_version must be 1 (historical) or 2 (reference edition)')
    require(bool(ep.get('fixture',False))==bool(fixture),'Historical fixtures require --fixture; normal news must not set fixture')
    day=date.fromisoformat(ep['date']);cutoff=iso(ep['cutoff']);now=datetime.now(TZ)
    require(day==cutoff.astimezone(TZ).date(),'Episode date differs from Beijing cutoff date')
    if not fixture:
        require(version==2,'New daily production requires schema 2; schema 1 is historical fixture only')
        require(cutoff>=now-timedelta(hours=24),'Frozen generation start is over 24 hours old; this build-start freshness check does not limit source age. Start a new editorial selection with a real current cutoff; revisions keep the original cutoff and predecessor')
        require(cutoff<=now,'Cutoff cannot be in the future')
    for key in ['title','description']:
        require(isinstance(ep.get(key),str) and ep[key].strip(),f'Missing {key}')
    sources=ep['sources'];claims=ep['claims'];scenes=ep['scenes']
    require(sources and claims and (3<=len(scenes)<=62 if version==2 else 2<=len(scenes)<=12),'Need sources, claims, and a complete episode within supported scene count')
    ss=safe_ids(sources,'source');cc=safe_ids(claims,'claim');safe_ids(scenes,'scene')
    require(set(ep['title_claim_ids'])<=cc,'Unknown title claim')
    require(ep['title_claim_ids'],'Map all title and description factual claims')
    source_map={s['id']:s for s in sources};snapshots={};source_text={}
    for s in sources:
        local_fixture=fixture and s.get('type')=='fixture'
        require(s.get('read_status')==('fixture-local' if local_fixture else 'opened'),'Source must actually have been opened: '+s['id'])
        require(s.get('type') in (['primary','independent-report','fixture'] if fixture else ['primary','independent-report']),'Invalid source type')
        require(s.get('publisher') and re.match(r'^https?://[^/\s]+',s['url']),'Source requires publisher and direct URL')
        published=date.fromisoformat(s['published_date'])
        if s.get('published_at'):require(iso(s['published_at'])<=cutoff,'Source published after cutoff: '+s['id'])
        else:require(published<=day,'Future source publication date: '+s['id'])
        fetched=iso(s['fetched_at'])
        if not fixture:require(fetched<=now+timedelta(minutes=5),'Source fetch timestamp is future')
        if fixture and not continuity_enabled(profile):
            age=(day-published).days
            require(age<=3 or s.get('context_only') is True,'Old news needs context_only; do not use as new headline')
            if age>1:require(s.get('recency_label')=='近期更新' or s.get('context_only') is True,'Older source requires explicit recency label')
        snap=(Path(path).parent/s['snapshot']).resolve()
        require(snap.is_file(),f'Missing actual source snapshot: {snap}')
        source_text[s['id']]=snap.read_text(encoding='utf-8-sig')
        require(len(source_text[s['id']].strip())>30,'Snapshot empty or too short')
        snapshots[str(snap)]=digest(snap)
    normalize=lambda text:re.sub(r'\s+',' ',text).strip()
    normalized_sources={sid:normalize(text) for sid,text in source_text.items()}
    for c in claims:
        require(c.get('status')=='verified' and c.get('text'),'Unverified claim: '+c['id'])
        require('limits' in c and isinstance(c['limits'],str),'Record applicable limits or explicit none: '+c['id'])
        require(c.get('evidence'),'Claim lacks evidence: '+c['id'])
        for e in c['evidence']:
            require(e['source_id'] in ss,'Unknown claim source')
            require(len(e.get('excerpt','').strip())>=8,'Evidence excerpt is too short')
            require(normalize(e['excerpt']) in normalized_sources[e['source_id']],'Evidence excerpt not in snapshot: '+c['id'])
    layouts={'overview','news','closing'} if version==2 else {'cover','big_text','stat','date_event','flow','conditions','closing'}
    if version==2:
        require(scenes[0]['layout']=='overview' and scenes[-1]['layout']=='closing','Start with overview; finish with independent closing segment')
        require(all(s['layout']=='news' for s in scenes[1:-1]),'Only news scenes belong between overview and closing')
        require(bool(ep.get('editorial_summary','').strip()),'Record the headline choice and whole-episode ordering rationale')
    for s in scenes:
        require(s.get('layout') in layouts,'Unknown layout')
        require(isinstance(s.get('head'),list) and 1<=len(s['head'])<=2,'Use 1-2 headline lines')
        require(s.get('speech','').strip() and s.get('section') and s.get('source_label'),'Missing scene text')
        require(set(s.get('source_ids',[]))<=ss and set(s.get('claim_ids',[]))<=cc,'Unknown scene source/claim')
        if not s.get('claim_ids'):require(s.get('editorial_only') is True,'Factual scene must cite claims')
        cited={e['source_id'] for c in claims if c['id'] in s.get('claim_ids',[]) for e in c['evidence']}
        require(cited<=set(s['source_ids']),'Scene sources must include claim evidence sources')
        require(isinstance(s.get('visual'),dict),'Missing visual data')
        if fixture and not continuity_enabled(profile) and any(source_map[i].get('recency_label')=='近期更新' for i in s['source_ids']):
            require('近期' in s['source_label'] or s['layout'] in ['cover','overview','closing'],'Label older news on its visual')
        if version==2:
            validate_reference_scene(s,ep,path,ss,cc)
    if version==2:
        news_claims={cid for s in scenes[1:-1] for cid in s['claim_ids']}
        require(news_claims<=set(scenes[0]['claim_ids']),'Overview contains news titles: map all news claims to overview')
        news_sources={sid for s in scenes[1:-1] for sid in s['source_ids']}
        require(news_sources<=set(scenes[0]['source_ids']),'Overview must carry the sources of its generated news list')
    from evidence_contract import enabled,validate_editorial_input
    retrospective,records=validate_editorial_input(ep,path,profile)
    snapshots.update({str(record):digest(record) for record in records})
    snapshots.update({str(anchor):digest(anchor) for anchor in window.anchors})
    if not fixture or enabled(profile) or retrospective or continuity_enabled(profile):
        validate_news_window(ep,require_core=enabled(profile),retrospectives=retrospective,window=window)
    return snapshots

def plain_caption(text):
    value=re.sub(r'\s+','',str(text))
    value=re.sub(r'[。！？；、!?;]+','',value)
    # Decimal/version separators and clock punctuation are factual content.
    return re.sub(r'(?<!\d)[，,．.：:]|[，,．.：:](?!\d)','',value)

def validate_reference_scene(s,ep,path,ss,cc):
    require(len(s['head'])==1,'Reference edition uses one headline string; fit before render')
    units=s.get('speech_units')
    require(isinstance(units,list) and units,'Reference edition requires measured speech_units, not character-weight subtitle estimates')
    for u in units:
        require(isinstance(u,dict) and all(isinstance(u.get(k),str) and u[k].strip() for k in ['text','display']),'Every speech unit needs spoken text and normative display text')
    require(plain_caption(''.join(u['display'] for u in units))==plain_caption(s['speech']),'speech must match the complete normative display text; phonetic text is separate')
    if s['layout']!='news':
        require(not s.get('media'),'Overview and closing have no separate media overlays')
        return
    require(s.get('nav_label','').strip(),'News needs a short bottom navigation label')
    editorial=s.get('editorial',{})
    for key in ['subject','event_key','state','change_since_previous','angle','order_reason']:
        require(isinstance(editorial.get(key),str) and editorial[key].strip(),'Record editorial.'+key+' for '+s['id'])
    cards=s['visual'].get('cards')
    require(isinstance(cards,list) and 1<=len(cards)<=6,'Each news page needs 1-6 useful cards')
    for card in cards:
        require(isinstance(card.get('title'),str) and card['title'].strip(),'Card needs an informative aspect title')
        require(isinstance(card.get('body'),str) and card['body'].strip(),'Card needs body text')
        require(card.get('claim_ids') and set(card['claim_ids'])<=set(s['claim_ids']),'Map every card including unspoken details to the scene claims')
    media=s.get('media',[])
    safe_ids(media,'media')
    for m in media:
        require(m.get('type') in ['image','video'],'Media type must be image or video')
        require(m.get('fit','contain')=='contain','Source media retains its aspect ratio')
        require(m.get('claim_ids') and set(m['claim_ids'])<=set(s['claim_ids']),'Media needs claims within its news scene')
        require(m.get('source_ids') and set(m['source_ids'])<=set(s['source_ids']),'Media needs sources within its news scene')
        require((Path(path).parent/m['path']).resolve().is_file(),'Missing actual media asset: '+m['path'])
        require(float(m.get('trim_start',0))>=0,'Negative video trim start')
        explicit='start' in m or 'end' in m
        require((all(k in m for k in ['start','end']) and not m.get('cue')) if explicit else bool(m.get('cue')),'Media needs either start/end seconds or an exact caption cue, never both')
        if explicit:require(0<=float(m['start'])<float(m['end']),'Media interval must be positive')
        else:
            require(isinstance(m['cue'],str) and plain_caption(m['cue']),'Media cue must contain meaningful caption text')
            if 'cue_end' in m:require(isinstance(m['cue_end'],str) and plain_caption(m['cue_end']),'Media cue_end must contain meaningful caption text')

def resolve_media(ep,audio):
    """Bind semantic cues after synthesis; never guess time from characters."""
    segments={x['id']:x for x in audio['timeline']}
    for s in ep['scenes']:
        segment=segments[s['id']]
        duration=float(segment['end'])-float(segment['start'])
        captions=[c for c in audio['captions'] if float(c['start'])>=float(segment['start'])-.002 and float(c['end'])<=float(segment['end'])+.002]
        def cue(text):
            require(bool(plain_caption(text)),'Media cue must contain meaningful caption text')
            found=[c for c in captions if plain_caption(text) in plain_caption(c['text'])]
            require(len(found)==1,f"Cue must match exactly one caption in {s['id']}: {text}")
            return found[0]
        for m in s.get('media',[]):
            if m.get('cue'):
                first=cue(m['cue']);last=cue(m.get('cue_end',m['cue']))
                m['start']=float(first['start'])-float(segment['start'])
                m['end']=float(last['end'])-float(segment['start'])
                m['timing_method']='caption_cue'
            else:m['timing_method']='editor_seconds'
            require(0<=m['start']<m['end']<=duration+.002,'Media timing lies outside its actual scene: '+m['id'])
        ordered=sorted(s.get('media',[]),key=lambda x:x['start'])
        require(all(a['end']<=b['start']+.002 for a,b in zip(ordered,ordered[1:])),'Arrange media sequentially; accidental overlapping source overlays are not allowed')

def select_root(profile):
    errors=[]
    for candidate in [profile['output_root'],profile['fallback_root']]:
        p=Path(candidate)
        try:
            anchor=p if p.exists() else Path(p.anchor)
            if not anchor.exists():raise OSError('drive unavailable')
            required=max(10*1024**3,2*int(profile.get('estimated_output_bytes',2*1024**3)))
            require(shutil.disk_usage(anchor).free>=required,'Less than 10 GiB or twice estimated output size free')
            p.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryFile(dir=p):pass
            return p,errors
        except (OSError,ValueError) as ex:errors.append(f'{candidate}: {ex}')
    raise ValueError('Neither output location usable: '+'; '.join(errors))

def doctor(profile):
    import importlib.util
    modules=['numpy','soundfile','sherpa_onnx','imageio_ffmpeg','PIL']
    missing=[m for m in modules if importlib.util.find_spec(m) is None]
    root=Path(profile['runtime_root'])
    model=root/'models/kokoro-int8-multi-lang-v1_1'
    files=[root/profile['python'],root/profile['font_sans'],Path(profile['font_serif'])]+[model/p for p in ['model.int8.onnx','voices.bin','tokens.txt','lexicon-us-en.txt','lexicon-zh.txt','espeak-ng-data']]
    if profile.get('voice',{}).get('engine')=='qwen3-tts':
        voice=profile['voice'];qmodel=root/voice['model_dir']
        files=[root/profile['python'],root/profile['font_sans'],Path(profile['font_serif']),root/voice['python']]+[qmodel/p for p in ['model.safetensors','speech_tokenizer/model.safetensors','config.json']]
    missing.extend(str(p) for p in files if not p.exists())
    output,notes=select_root(profile)
    require(not missing,'Missing local dependencies: '+', '.join(missing))
    delivery_root=profile.get('delivery',{}).get('root')
    if delivery_root:
        destination=Path(delivery_root)
        require(destination.is_absolute(),'Delivery root must be absolute')
        destination.mkdir(parents=True,exist_ok=True)
        require(shutil.disk_usage(destination).free>=2*int(profile.get('estimated_output_bytes',2*1024**3)),
                'Insufficient space at explicitly selected delivery root: '+str(destination))
        with tempfile.TemporaryFile(dir=destination):pass
    return {'status':'ready','beijing_time':datetime.now(TZ).isoformat(),'runtime_root':str(root),'output_root':str(output),'delivery_root':delivery_root,'fallback_notes':notes,'profile_version':profile['version']}

def bulletin_label(profile):
    return (profile or {}).get('delivery',{}).get('bulletin_label','早报')

def publication(ep,profile=None,path=None):
    from news_window import enabled as continuity_enabled,resolve_window,window_copy
    text=f"# {ep['title']}\n\n"
    if (profile or {}).get('delivery',{}).get('title_format')=='dual-original-short':
        text+=f"短标题（20字符以内）：{ep['title_short']}\n\n"
    text+=f"{ep['description']}\n\n#AI{bulletin_label(profile)} #人工智能 #AI工具\n\n"
    cutoff=iso(ep['cutoff']).astimezone(TZ)
    text+='## 来源与时间\n\n采编截至 '+cutoff.isoformat()+'（北京时间）。\n\n'
    if not ep.get('fixture') or continuity_enabled(profile):
        text+=window_copy(resolve_window(ep,profile,path))
    for retrospective in ep.get('retrospectives',[]):
        period='本期普通新闻窗口内' if continuity_enabled(profile) else '近24小时'
        text+='专题回顾：原始公开日期 '+retrospective['publication_date']+'；本段为历史专题，不计作'+period+'新披露。\n\n'
    for s in ep['sources']:
        text+=f"- [{s['publisher']}]({s['url']})：{s.get('published_at') or s['published_date']}"+('，历史背景' if s.get('context_only') is True else '')+'。\n'
    text+='\n## 本期口播\n\n'+'\n\n'.join(s['speech'] for s in ep['scenes'])
    text+='\n\n配音和信息图由AI辅助生成；资料图片与演示的出处见上方来源。\n'
    return text

def validate_delivery_title(ep,profile):
    config=profile.get('delivery',{})
    if config.get('title_format')=='dual-original-short':
        require(isinstance(ep.get('title'),str) and ep['title']==ep['title'].strip() and len(ep['title'].splitlines())==1,'Provide one complete original-format title line in title')
        require(isinstance(ep.get('title_short'),str) and ep['title_short'].strip(),'Provide the short title in title_short as well as the original-format title')
        validate_delivery_title(ep,{'delivery':{**config,'title_format':'headlines-bracket-date'}})
        require(len(ep['title'].split('；'))<=2,'Original title should summarize one or two headlines')
        validate_delivery_title({**ep,'title':ep['title_short']},
                                {'delivery':{**config,'title_format':'single-hotspot-date'}})
        return
    if config.get('title_format')=='single-hotspot-date':
        day=date.fromisoformat(ep['date']);title=ep['title']
        suffix=f'｜{day.month}月{day.day}日{bulletin_label(profile)}'
        limit=config['title_max_chars']
        require(isinstance(title,str) and title==title.strip() and len(title.splitlines())==1,'Provide one complete title line without outer whitespace')
        require(len(title)<=limit,f'Complete title must be at most {limit} characters, including punctuation, spaces, letters and digits')
        require(title.endswith(suffix),'Title must end with '+suffix)
        body=title[:-len(suffix)]
        require(bool(body.strip()) and title.count('｜')==1,'Provide one hotspot followed by the episode date')
        require('；' not in body and ';' not in body,'Use one hotspot, not a semicolon-separated headline list')
        return
    if config.get('title_format')!='headlines-bracket-date':return
    suffix=f"【AI{bulletin_label(profile)} {ep['date']}】"
    title=ep['title']
    require(title.endswith(suffix),'Title must end with '+suffix)
    body=title[:-len(suffix)].strip()
    require(bool(body) and '\n' not in title and '\r' not in title,'Provide one complete headline title line')
    require(all(part.strip() for part in body.split('；')),'Do not leave empty headline slots')

def delivery_title_text(ep,profile):
    validate_delivery_title(ep,profile)
    if profile.get('delivery',{}).get('title_format')=='dual-original-short':
        return f"原标题：\n{ep['title']}\n\n短标题（20字符以内）：\n{ep['title_short']}\n"
    return ep['title']+'\n'

def delivery_directory(profile,day,legacy_root):
    config=profile.get('delivery',{})
    if not config.get('root'):
        base=Path(legacy_root)/day;base.mkdir(parents=True,exist_ok=True)
        index=1
        while (base/f'v{index:02}').exists():index+=1
        final=base/f'v{index:02}';final.mkdir()
        return final,index
    stamp=date.fromisoformat(day);root=Path(config['root'])
    require(root.is_absolute(),'Delivery root must be absolute')
    # An explicitly selected destination must not silently fall back elsewhere.
    root.mkdir(parents=True,exist_ok=True)
    base=root/config['folder_format'].format(month=stamp.month,day=stamp.day,year=stamp.year)
    if base.exists():
        recorded={m.group(1) for p in base.rglob('AI*_v*.mp4')
                  if (m:=re.fullmatch(r'AI(?:新闻|[早晚]报)_(\d{4}-\d{2}-\d{2})_v\d+\.mp4',p.name))}
        if recorded and recorded!={day}:base=root/(base.name+f'（{stamp.year}年）')
    base.mkdir(parents=True,exist_ok=True)
    if not any(base.iterdir()):return base,1
    index=2
    while (base/f'v{index:02}').exists():index+=1
    final=base/f'v{index:02}';final.mkdir()
    return final,index

def check_audio_quality(audio):
    quality=audio['quality']
    for key in ['caption_alignment_pass','caption_timing_valid','no_clipped_samples','loudness_pass','true_peak_pass','duration_preserved','duration_pass']:
        require(quality.get(key) is True,'Audio technical check failed: '+key)
    methods={'measured_synthesis_units','measured_units_with_editor_boundaries'}
    require(audio.get('caption_alignment_method') in methods and quality.get('caption_alignment_method') in methods,'Reference edition cannot use estimated caption timing')

def assert_frozen(tracked):
    for name,value in tracked.items():
        require(Path(name).is_file() and digest(name)==value,'Input/configuration/source changed during build: '+name)

def reuse_visual_video(source_work,ep,audio,work,profile):
    """Copy a verified, unchanged visual stream for an audio-only revision."""
    import time
    import imageio_ffmpeg
    from audio_pipeline import _run
    started=time.perf_counter()
    require(profile.get('delivery_qa',{}).get('enabled') is True,'Visual reuse requires final encoded-media QA')
    prior=Path(source_work).resolve()
    require(prior!=work,'Visual reuse needs a different prior work folder')
    m=read(prior/'manifest.json');receipt=read(prior/'delivery.json')
    require(receipt['manifest_sha256']==digest(prior/'manifest.json'),'Prior delivery manifest changed')
    require(receipt['review_sha256']==digest(prior/'review.json'),'Prior delivery review changed')
    require(read(prior/'review.json')['status']=='passed','Visual reuse requires a reviewed delivery')
    required=[prior/x for x in ['episode.json','render-episode.json','profile.json','audio-report.json','video-report.json']]
    old_media=m['media']
    required += [Path(old_media[k]) for k in ['video_path','cover_path','srt_path','contact_path']]
    for path in required:
        require(m['tracked'].get(str(path))==digest(path),'Prior visual input/output changed: '+str(path))
    require(read(prior/'render-episode.json')==ep,'Visual reuse requires identical resolved editorial and media content')
    old_profile=read(prior/'profile.json')
    for key in ['visual','video','font_sans','font_serif']:
        require(old_profile.get(key)==profile.get(key),'Visual reuse profile differs: '+key)
    old_audio=read(prior/'audio-report.json')
    for key in ['timeline','captions','duration']:
        require(old_audio[key]==audio[key],'Visual reuse requires unchanged narration/caption timing: '+key)
    for scene in ep['scenes']:
        for asset in scene.get('media',[]):
            path=Path(asset['path'])
            require(m['tracked'].get(str(path))==digest(path),'Visual reuse source media changed: '+str(path))
            required.append(path)
    old_video=Path(old_media['video_path'])
    require(receipt['sha256'][Path(receipt['video']).name]==digest(old_video),'Prior published and rendered videos differ')
    frozen={str(p):digest(p) for p in required+[prior/'manifest.json',prior/'review.json',prior/'delivery.json']}
    output=work/old_video.name
    _run(imageio_ffmpeg.get_ffmpeg_exe(),['-y','-v','error','-i',old_video,'-i',audio['paths']['mix'],
          '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','256k',
          '-t',f"{audio['duration']:.9f}",'-movflags','+faststart',output])
    media=json.loads(json.dumps(old_media))
    media['video_path']=str(output)
    media['frames']=[]  # New encoded-media frames are produced by final QA.
    media['metrics']['sha256']=digest(output)
    media['metrics']['seconds_to_render']=time.perf_counter()-started
    media['metrics']['decode']='pending_final_qa'
    for key in ['cover_path','srt_path','contact_path']:
        source=Path(old_media[key]);target=work/source.name
        shutil.copy2(source,target);media[key]=str(target)
    media['visual_reuse']={'source_work':str(prior),'source_video_sha256':digest(old_video),
                           'method':'Unchanged H264 stream copied; new AAC mixed and encoded; final QA still required',
                           'inherited_metrics':'Layout geometry, source-media metadata and overview_scroll paths describe the verified prior visual render. Output hash and build time are new; final-qa provides new decode, audio and frame evidence.'}
    assert_frozen(frozen)
    return media,frozen

def build(args,profile):
    status=doctor(profile);root=Path(status['runtime_root']);output=Path(status['output_root'])
    ep_path=Path(args.episode).resolve()
    frozen={str(p):digest(p) for p in [ep_path,Path(args.profile).resolve(),*SKILL.joinpath('scripts').glob('*.py')]}
    transition=profile.get('transition_sound',{})
    if transition.get('enabled') and transition.get('asset_path'):
        click_path=Path(transition['asset_path'])
        if not click_path.is_absolute():click_path=SKILL/click_path
        click_path=click_path.resolve()
        require(click_path.is_file(),'Missing configured transition sample: '+str(click_path))
        require(digest(click_path)==transition.get('asset_sha256'),'Transition sample hash mismatch')
        frozen[str(click_path)]=digest(click_path)
    require(read(args.profile)==profile,'Profile changed after reading; restart with one consistent profile')
    ep=read(ep_path);snapshots=validate(ep,ep_path,args.fixture,profile)
    from news_window import enabled as continuity_enabled,resolve_window
    window=resolve_window(ep,profile,ep_path)
    if continuity_enabled(profile) and window.previous_delivery:
        ep['news_window']['previous_delivery']=str(window.previous_delivery)
    validate_delivery_title(ep,profile)
    require(ep['schema_version']==profile.get('episode_schema',1),'Episode/profile schema mismatch; historical layout must use its saved profile')
    # The copied episode remains self-contained for reviewers in a fresh task.
    for source in ep['sources']:
        source['snapshot']=str((ep_path.parent/source['snapshot']).resolve())
    if ep.get('editorial_research'):
        ep['editorial_research']=str((ep_path.parent/ep['editorial_research']).resolve())
    for retrospective in ep.get('retrospectives',[]):
        auth=retrospective['authorization'];auth['record']=str((ep_path.parent/auth['record']).resolve())
    if profile.get('voice',{}).get('engine')=='qwen3-tts':
        from qwen_voice import prepare_scene_audio
        prepare_scene_audio(ep['scenes'],root,output/'work/qwen-audio-cache',profile['voice'])
    asset_hashes={}
    for scene in ep['scenes']:
        for unit in scene.get('speech_units',[]):
            if 'audio_path' in unit:
                unit['audio_path']=str((ep_path.parent/unit['audio_path']).resolve())
                require(Path(unit['audio_path']).is_file(),'Missing prebuilt speech')
                require(digest(unit['audio_path'])==unit.get('audio_sha256'),'Prebuilt speech hash mismatch')
                asset_hashes[unit['audio_path']]=digest(unit['audio_path'])
        for asset in scene.get('media',[]):
            asset['path']=str((ep_path.parent/asset['path']).resolve())
            asset_hashes[asset['path']]=digest(asset['path'])
    work=Path(args.work).resolve() if args.work else output/'work'/ep['date']/datetime.now().strftime('%H%M%S')
    work.mkdir(parents=True,exist_ok=True)
    require(not (work/'manifest.json').exists(),'Use a fresh work folder for a new build; reuse shared audio caches automatically')
    write(work/'episode.json',ep);write(work/'profile.json',profile)
    from editorial_checks import pronunciation_inventory,factual_inventory
    from evidence_contract import enabled,review_template
    write(work/'pronunciation-inventory.json',pronunciation_inventory(ep))
    if enabled(profile):write(work/'factual-inventory.json',factual_inventory(ep))
    from audio_pipeline import build_audio
    from video_pipeline import render_video
    audio=build_audio(ep['scenes'],root,work,{**profile,'cache_dir':str(output/'work/audio-cache')})
    write(work/'audio-report.json',audio)
    require(audio['quality']['duration_pass'],f"Narration is {audio['duration']:.2f}s; edit script to {profile['video']['min_seconds']}-{profile['video']['max_seconds']}s, keep natural speed")
    if ep['schema_version']==2:
        check_audio_quality(audio)
        resolve_media(ep,audio)
    write(work/'render-episode.json',ep)
    if args.reuse_video_work:
        media,reused=reuse_visual_video(args.reuse_video_work,ep,audio,work,profile)
        asset_hashes.update(reused)
    else:
        media=render_video(ep,audio,root,work,profile)
    write(work/'video-report.json',media)
    final_qa=None
    if profile.get('delivery_qa',{}).get('enabled'):
        from delivery_qa import inspect_delivery
        final_qa=inspect_delivery(ep,audio,media,work,profile)
        write(work/'final-qa.json',final_qa)
        require(final_qa.get('technical_pass') is True,'Final encoded audio or transition check failed; inspect final-qa.json')
        media['metrics']['decode']='passed'
        write(work/'video-report.json',media)
    copy=work/'发布文案与来源.md';copy.write_text(publication(ep,profile),encoding='utf-8')
    title_path=None
    if profile.get('delivery',{}).get('title_format') in ('headlines-bracket-date','single-hotspot-date','dual-original-short'):
        title_path=work/'标题.txt';title_path.write_text(delivery_title_text(ep,profile),encoding='utf-8')
    if args.fixture:
        copy.write_text('历史回归样片，禁止作为今日新闻发布。\n\n'+copy.read_text(encoding='utf-8'),encoding='utf-8')
    assert_frozen({**frozen,**snapshots,**asset_hashes})
    tracked={str(p):digest(p) for p in [work/'episode.json',work/'render-episode.json',work/'profile.json',copy,work/'audio-report.json',work/'video-report.json']}
    tracked[str(work/'pronunciation-inventory.json')]=digest(work/'pronunciation-inventory.json')
    if enabled(profile):tracked[str(work/'factual-inventory.json')]=digest(work/'factual-inventory.json')
    if title_path is not None:tracked[str(title_path)]=digest(title_path)
    if final_qa is not None:
        tracked[str(work/'final-qa.json')]=digest(work/'final-qa.json')
        for p in (work/'final-qa').rglob('*'):
            if p.is_file() and p.suffix.lower() in ('.json','.jpg','.png'):tracked[str(p)]=digest(p)
    for key in ['video_path','cover_path','srt_path','contact_path']:
        p=Path(media[key]);tracked[str(p)]=digest(p)
    tracked.update(frozen)
    tracked.update(snapshots)
    tracked.update(asset_hashes)
    manifest={'schema_version':1,'fixture':args.fixture,'episode_date':ep['date'],'profile_version':profile['version'],'profile_source':str(Path(args.profile).resolve()),'profile_sha256':digest(args.profile),'input':str(ep_path),'work':str(work),'output_root':str(output),'audio':audio,'media':media,'tracked':tracked,'created':datetime.now(TZ).isoformat()}
    if continuity_enabled(profile):manifest['news_window']=window.record()
    write(work/'manifest.json',manifest)
    review={'manifest_sha256':digest(work/'manifest.json'),'status':'pending','claim_checks':{c['id']:'pending' for c in ep['claims']},'scene_checks':{s['id']:'pending' for s in ep['scenes']},'title_cover_copy_verified':False,'source_dates_and_limits_verified':False,'pronunciation_and_subtitle_notes':'','audio_review_method':'pending','asr_sha256':'','reviewer':'','issues':[]}
    if ep['schema_version']==2:
        review.update({'editorial_order_notes':'','news_state_and_grouping_verified':False,'cards_and_media_facts_verified':False,'caption_timing_verified':False,'navigation_and_transitions_verified':False,'media_checks':{s['id']+'/'+a['id']:'pending' for s in ep['scenes'] for a in s.get('media',[])}})
    if profile.get('delivery_qa',{}).get('enabled'):
        review.update({'final_media_qa_verified':False,'pronunciation_inventory_reviewed':False,
                       'media_selection_notes':{s['id']:'' for s in ep['scenes'] if s['layout']=='news'}})
    if profile.get('delivery_qa',{}).get('social_source_review_required'):
        review['social_source_notes']=''
    if enabled(profile):review.update(review_template(ep))
    write(work/'review.json',review)
    print(json.dumps({'status':'rendered-needs-agent-review','work':str(work),'video':media['video_path'],'contact':media['contact_path'],'review':str(work/'review.json'),'duration':audio['duration'],'fallback_notes':status['fallback_notes']},ensure_ascii=False,indent=2))

def check_navigation_labels(ep,metrics):
    news_ids={scene['id'] for scene in ep['scenes'] if scene.get('layout')=='news'}
    blocked={note.get('id') for note in metrics.get('navigation_notes',[])
             if note.get('status')=='label-does-not-fit' and note.get('id') in news_ids}
    require(not blocked,'Shorten news navigation labels and rerender; labels did not fit: '+', '.join(sorted(blocked)))


def verify_review(work):
    mf=work/'manifest.json';m=read(mf);review=read(work/'review.json');ep=read(work/'episode.json')
    require(not m['fixture'],'Historical QA fixtures cannot be published')
    require(digest(m['profile_source'])==m['profile_sha256'],'Profile changed; rebuild and review')
    for name,h in m['tracked'].items():require(Path(name).is_file() and digest(name)==h,'Changed or missing reviewed artifact: '+name)
    saved_profile=read(work/'profile.json')
    from news_window import enabled as continuity_enabled,resolve_window
    window=resolve_window(ep,saved_profile,work/'episode.json')
    if continuity_enabled(saved_profile):
        require(m.get('news_window')==window.record(),'Frozen news window differs from its saved profile and predecessor')
        for anchor in window.anchors:
            require(m['tracked'].get(str(anchor))==digest(anchor),'Previous delivery anchor not frozen in this render: '+str(anchor))
    from evidence_contract import enabled,validate_editorial_input,validate_editorial_review
    retrospective,records=validate_editorial_input(ep,work/'episode.json',saved_profile)
    for record in records:
        require(m['tracked'].get(str(record))==digest(record),'Editorial/authorization record not frozen in this render: '+str(record))
    validate_news_window(ep,require_core=enabled(saved_profile),retrospectives=retrospective,window=window)
    require(review['manifest_sha256']==digest(mf),'Review is stale for current render')
    require(review['status']=='passed' and review['reviewer'].strip(),'Agent review still pending')
    require(review['claim_checks']=={c['id']:'verified' for c in ep['claims']},'All claims must be actually checked')
    require(review['scene_checks']=={s['id']:'verified' for s in ep['scenes']},'Inspect each rendered scene')
    require(review['title_cover_copy_verified'] is True and review['source_dates_and_limits_verified'] is True,'Public text/date/limits review missing')
    require(not review['issues'],'Resolve known issues before publishing package')
    if ep['schema_version']==2:
        check_audio_quality(m['audio'])
        check_navigation_labels(ep,m.get('media',{}).get('metrics',{}))
        require(all(review.get(k) is True for k in ['news_state_and_grouping_verified','cards_and_media_facts_verified','caption_timing_verified','navigation_and_transitions_verified']),'Reference edition editorial/visual/timing checks still pending')
        require(len(review.get('editorial_order_notes','').strip())>=12,'Record actual headline/grouping/state checks')
        require(review.get('media_checks')=={s['id']+'/'+a['id']:'verified' for s in ep['scenes'] for a in s.get('media',[])},'Inspect every media overlay, including real motion and readable key details')
    validate_delivery_title(ep,saved_profile)
    if enabled(saved_profile):
        require(m['tracked'].get(str(work/'factual-inventory.json'))==digest(work/'factual-inventory.json'),'Factual inventory missing from frozen render')
        validate_editorial_review(ep,review)
    if saved_profile.get('delivery',{}).get('title_format') in ('headlines-bracket-date','single-hotspot-date','dual-original-short'):
        require((work/'标题.txt').read_text(encoding='utf-8').strip()==delivery_title_text(ep,saved_profile).rstrip('\n'),'Reviewed title file differs from episode titles')
    qa_policy=saved_profile.get('delivery_qa',{})
    if qa_policy.get('enabled'):
        qa=read(work/'final-qa.json')
        require(qa.get('technical_pass') is True,'Final encoded-media technical checks failed')
        require(qa.get('video_sha256')==digest(m['media']['video_path']),'Final QA does not match this video')
        require(review.get('final_media_qa_verified') is True,'Inspect final media entry/exit and navigation frames')
    if qa_policy.get('pronunciation_inventory_required'):
        require(review.get('pronunciation_inventory_reviewed') is True,'Review the pronunciation inventory against actual audio')
    if qa_policy.get('media_selection_review_required'):
        notes=review.get('media_selection_notes',{})
        require(set(notes)=={s['id'] for s in ep['scenes'] if s['layout']=='news'},'Record media selection or omission for each news item')
        require(all(isinstance(v,str) and len(v.strip())>=8 for v in notes.values()),'Media notes must explain the actual selection or reasonable omission')
    if qa_policy.get('social_source_review_required'):
        social_notes=review.get('social_source_notes','')
        require(isinstance(social_notes,str) and len(social_notes.strip())>=12,'Record actual social/forum and comment coverage, findings or access limits')
    require(review['audio_review_method'] in ['listened-and-signal','asr-and-signal'],'Audio inspection method missing; do not invent a listening pass')
    require(len(review['pronunciation_and_subtitle_notes'].strip())>=12,'Record actual pronunciation and estimated subtitle checks')
    if review['audio_review_method']=='asr-and-signal':
        require((work/'asr-review.json').is_file(),'Run local final-mix ASR and inspect transcript before claiming ASR review')
        report=read(work/'asr-review.json')
        require(review.get('asr_sha256')==digest(work/'asr-review.json'),'ASR transcript changed since review')
        require(report.get('audio_sha256')==digest(m['media']['video_path']),'ASR report does not match final encoded video')
        require({r['scene_id'] for r in report['scenes']}=={s['id'] for s in ep['scenes']},'ASR review must cover every scene')
        require(all(r['recognized'].strip() for r in report['scenes']),'Empty scene transcription needs investigation')
    return m,review,ep

def publish(args):
    work=Path(args.work).resolve();m,review,ep=verify_review(work)
    profile=read(work/'profile.json')
    validate_delivery_title(ep,profile)
    final,index=delivery_directory(profile,ep['date'],m['output_root'])
    video_name=f"AI{bulletin_label(profile)}_{ep['date']}_v{index:02}.mp4"
    for key,name in [('video_path',video_name),('cover_path','封面.png'),('srt_path','字幕.srt')]:shutil.copy2(m['media'][key],final/name)
    shutil.copy2(work/'发布文案与来源.md',final/'发布文案与来源.md')
    if profile.get('delivery',{}).get('title_format') in ('headlines-bracket-date','single-hotspot-date','dual-original-short'):
        shutil.copy2(work/'标题.txt',final/'标题.txt')
    archive=final/f"AI{bulletin_label(profile)}_{ep['date']}_发布包.zip"
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in final.iterdir():
            if p!=archive and p.is_file():z.write(p,p.name)
    with zipfile.ZipFile(archive) as z:require(z.testzip() is None,'Corrupt zip')
    receipt={'date':ep['date'],'version':index,'title':ep['title'],'directory':str(final),'video':str(final/video_name),'zip':str(archive),'manifest_sha256':digest(work/'manifest.json'),'review_sha256':digest(work/'review.json'),'sha256':{p.name:digest(p) for p in final.iterdir() if p.is_file()}}
    if profile.get('delivery',{}).get('title_format')=='dual-original-short':
        receipt['title_short']=ep['title_short']
    write(work/'delivery.json',receipt);print(json.dumps(receipt,ensure_ascii=False,indent=2))

def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',default=str(SKILL/'config/profile.local.json'))
    sub=p.add_subparsers(dest='command',required=True);sub.add_parser('doctor')
    for name in ['validate','render']:
        q=sub.add_parser(name);q.add_argument('--episode',required=True);q.add_argument('--fixture',action='store_true')
        if name=='render':
            q.add_argument('--work')
            q.add_argument('--reuse-video-work',help='Reviewed prior work with identical visuals and timing; remux its video with new audio')
    q=sub.add_parser('publish');q.add_argument('--work',required=True)
    q=sub.add_parser('transcribe');q.add_argument('--work',required=True)
    q=sub.add_parser('inventory');q.add_argument('--episode',required=True);q.add_argument('--work',required=True);q.add_argument('--fixture',action='store_true')
    args=p.parse_args();profile=read(args.profile)
    if args.command=='doctor':print(json.dumps(doctor(profile),ensure_ascii=False,indent=2))
    elif args.command=='validate':
        episode=read(args.episode);snapshots=validate(episode,args.episode,args.fixture,profile);validate_delivery_title(episode,profile);print(json.dumps({'status':'structurally-valid-not-semantic-proof','source_snapshots':snapshots},ensure_ascii=False))
    elif args.command=='render':build(args,profile)
    elif args.command=='publish':publish(args)
    elif args.command=='inventory':
        from editorial_checks import pronunciation_inventory,factual_inventory
        episode=read(args.episode)
        require(isinstance(episode,dict),'Editorial inventories require a schema 2 episode object')
        require(episode.get('schema_version')==2,'Editorial inventories require schema 2 with complete sources, claims and speech units')
        try:validate(episode,args.episode,args.fixture,profile)
        except (KeyError,TypeError,AttributeError) as ex:
            raise ValueError('Invalid schema 2 inventory input; check required source, claim, scene and speech-unit fields: '+str(ex)) from ex
        work=Path(args.work).resolve();work.mkdir(parents=True,exist_ok=True)
        require(not (work/'manifest.json').exists(),'Do not overwrite a frozen render checklist; use its existing inventory')
        result=pronunciation_inventory(episode);write(work/'pronunciation-inventory.json',result)
        facts=factual_inventory(episode);write(work/'factual-inventory.json',facts)
        print(json.dumps({'inventory':str(work/'pronunciation-inventory.json'),'units':result['unit_count'],'expressions':len(result['terms']),'factual_inventory':str(work/'factual-inventory.json'),'claims':facts['claim_count']},ensure_ascii=False))
    else:
        from audio_pipeline import transcribe_audio
        work=Path(args.work).resolve();m=read(work/'manifest.json');a=read(work/'audio-report.json')
        result=transcribe_audio(m['media']['video_path'],Path(profile['runtime_root']),work,timeline=a['timeline'])
        result['audio_sha256']=digest(m['media']['video_path'])
        write(work/'asr-review.json',result)
        review=read(work/'review.json');review['asr_sha256']=digest(work/'asr-review.json');review['status']='pending'
        write(work/'review.json',review);print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:main()
    except Exception as ex:print(f'ERROR: {ex}',file=sys.stderr);sys.exit(1)
