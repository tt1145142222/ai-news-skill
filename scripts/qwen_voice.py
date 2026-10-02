"""Free local Qwen3-TTS worker, isolated from the renderer's Python environment.

The public prepare_scene_audio function only needs Python's standard library.
The child process uses the dedicated local Qwen runtime and checked model files.
"""
from pathlib import Path
import hashlib,json,os,subprocess,sys,time,uuid

VERSION='1.0.0'
STYLE='用标准普通话自然地讲述科技资讯。像一位熟悉这些产品的年轻男主播，在向朋友解释刚看到的新闻。清晰、轻松、专注，语速略快但不抢字，有正常的重音起伏和句尾收束。不要拖长尾音，不要逐字朗读，不要夸张播音腔。英文缩写按字母名读，英文产品名按英语发音。'
def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
def write(path,data):Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
def read(path):return json.loads(Path(path).read_text(encoding='utf8'))
def prepare_scene_audio(scenes,root,cache,voice):
 root,cache=Path(root),Path(cache);cache.mkdir(parents=True,exist_ok=True)
 model=(root/voice['model_dir']).resolve();python=(root/voice['python']).resolve()
 weights={name:sha(model/name) for name in ['model.safetensors','speech_tokenizer/model.safetensors','config.json','generation_config.json']}
 common={'worker_version':VERSION,'model_files':weights,'speaker':voice.get('speaker','Dylan'),'style':voice.get('style',STYLE),'temperature':voice.get('temperature',.7),'top_p':voice.get('top_p',.8),'top_k':voice.get('top_k',30),'repetition_penalty':voice.get('repetition_penalty',1.08)}
 all_units=[];pending=[]
 for s in scenes:
  for i,u in enumerate(s['speech_units']):
   identity={**common,'text':u['text']};key=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
   wav=cache/(key+'.wav');meta=wav.with_suffix('.json');valid=False
   if wav.is_file() and meta.is_file():
    try:
     info=read(meta);valid=info.get('identity')==identity and info.get('sha256')==sha(wav)
    except (ValueError,OSError):pass
   if not valid:pending.append({'path':str(wav),'identity':identity,'scene_id':s['id'],'unit_index':i})
   all_units.append((u,wav,identity))
 if pending:
  requests=cache/'requests';requests.mkdir(exist_ok=True)
  request=requests/(str(uuid.uuid4())+'.json')
  write(request,{'root':str(root),'model':str(model),'batch_size':int(voice.get('batch_size',4)),'units':pending})
  env=os.environ.copy();env.update(HF_HOME=str(root/'work/hf-cache'),HF_HUB_OFFLINE='1',TOKENIZERS_PARALLELISM='false',TORCH_HOME=str(root/'work/torch-cache'),PYTHONDONTWRITEBYTECODE='1')
  subprocess.run([str(python),'-u','-B',str(Path(__file__).resolve()),'--request',str(request)],check=True,cwd=root,env=env)
 for unit,wav,identity in all_units:
  info=read(wav.with_suffix('.json'))
  if info.get('identity')!=identity or info.get('sha256')!=sha(wav):raise ValueError('Stale or changed Qwen audio cache')
  unit.update(audio_path=str(wav),audio_sha256=info['sha256'],audio_provenance=info)
 return scenes

def run_worker(request):
 import torch,numpy as np,soundfile as sf
 from qwen_tts import Qwen3TTSModel
 data=read(request);torch.set_num_threads(6)
 if not torch.cuda.is_available():raise RuntimeError('Selected local Qwen voice requires the configured GPU; do not silently fall back to the rejected voice')
 model=Qwen3TTSModel.from_pretrained(data['model'],device_map='cuda:0',dtype=torch.bfloat16,attn_implementation='sdpa')
 size=data['batch_size']
 if not 1<=size<=4:raise ValueError('Qwen batch size must be 1–4')
 for offset in range(0,len(data['units']),size):
  batch=data['units'][offset:offset+size];settings=batch[0]['identity'];seed=int(hashlib.sha256(batch[0]['path'].encode()).hexdigest()[:8],16)
  torch.manual_seed(seed);torch.cuda.manual_seed_all(seed);started=time.time()
  with torch.inference_mode():
   wavs,rate=model.generate_custom_voice(text=[x['identity']['text'] for x in batch],language=['Chinese']*len(batch),speaker=[x['identity']['speaker'] for x in batch],instruct=[x['identity']['style'] for x in batch],max_new_tokens=600,do_sample=True,top_p=settings['top_p'],top_k=settings['top_k'],temperature=settings['temperature'],repetition_penalty=settings['repetition_penalty'])
  for row,wave in zip(batch,wavs):
   signal=np.asarray(wave,dtype=np.float32)
   if not signal.size or not np.isfinite(signal).all():raise ValueError('Invalid Qwen output')
   window=max(1,round(.01*rate));env=np.sqrt(np.convolve(signal*signal,np.ones(window)/window,mode='same'))
   active=np.flatnonzero(env>max(.0008,float(env.max())*.006))
   if active.size:signal=signal[max(0,int(active[0])-round(.09*rate)):min(len(signal),int(active[-1])+round(.16*rate))]
   if len(signal)/rate>35:raise ValueError('Unexpectedly long speech unit: inspect repetition or split only at a natural sentence')
   peak=float(np.max(np.abs(signal)))
   if peak>.93:signal*=.93/peak
   path=Path(row['path']);sf.write(path,signal,rate,subtype='PCM_24')
   info={'engine':'Qwen3-TTS-12Hz-1.7B-CustomVoice','identity':row['identity'],'sha256':sha(path),'duration':len(signal)/rate,'sample_rate':rate,'seed':seed,'batch_size':len(batch),'generation_batch_seconds':time.time()-started,'rate_manipulation':False,'cost':'local inference; no paid service'}
   write(path.with_suffix('.json'),info)
  print(f'Qwen local narration: {min(offset+size,len(data["units"]))}/{len(data["units"])} utterances',flush=True)
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--request',required=True);run_worker(p.parse_args().request)
