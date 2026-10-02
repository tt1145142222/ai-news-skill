"""Duration-driven 1080p newsroom. Pillow typography; FFmpeg video motion.

This module only renders local, reviewed inputs. It never fetches sources,
synthesizes speech or grants editorial approval.
"""
from __future__ import annotations
from collections import OrderedDict
from datetime import date
from pathlib import Path
import hashlib
import json
import math
import re
import time
import imageio_ffmpeg
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageFilter, ImageOps
from video_pipeline_legacy import LayoutError, _run, _resolve, _stamp

W, H, FPS = 1920, 1080, 30

def _rgb(value, alpha=None):
    value = ImageColor.getrgb(value)
    return value if alpha is None else (*value[:3], alpha)

def _sha(path):
    d = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''): d.update(block)
    return d.hexdigest()

def _head(scene):
    value = scene.get('head', '')
    return ' '.join(str(x).strip() for x in value) if isinstance(value,list) else str(value).strip()

class Layout:
    def __init__(self, root, profile):
        self.v = profile.get('visual', {})
        self.bg = self.v.get('background', '#F6F8FC')
        self.ink = self.v.get('ink', '#202938')
        self.accent = self.v.get('accent', '#2862B4')
        self.muted = self.v.get('muted', '#65758A')
        self.card_bg = self.v.get('card_background', '#FFFFFF')
        self.nav_bg = self.v.get('nav_background', '#E6EDF7')
        self.border = self.v.get('border', '#DFE6F0')
        self.radius = int(self.v.get('card_radius', 30))
        self.gap = int(self.v.get('card_gap', 28))
        self.colors = self.v.get('card_accents', ['#2862B4','#4585B7','#345A8A','#578A9C'])
        self.top = int(self.v.get('nav_top_height',self.v.get('navtop',52)))
        self.bottom = int(self.v.get('nav_bottom_height',self.v.get('navbottom',52)))
        self.progress_height = int(self.v.get('nav_progress_height',4))
        if not 36 <= self.top <= 64 or not 36 <= self.bottom <= 64:
            raise LayoutError('Navigation heights must stay within 36–64 pixels')
        if not 2 <= self.progress_height <= 8:
            raise LayoutError('Navigation progress height must stay within 2–8 pixels')
        self.font_path = _resolve(root,profile['font_sans'])
        self.fonts, self.boxes, self.navigation_notes = {}, [], []

    def font(self,size):
        size=int(size)
        if size not in self.fonts: self.fonts[size]=ImageFont.truetype(str(self.font_path),size)
        return self.fonts[size]

    def width(self,text,size,bold=False):
        return self.font(size).getlength(str(text))+(2 if bold and text else 0)

    def text(self,im,xy,text,size,fill=None,bold=False,tag='text',anchor='lt'):
        text=str(text)
        if not text.strip(): return
        x,y=map(round,xy); d=ImageDraw.Draw(im)
        d.text((x,y),text,font=self.font(size),anchor=anchor,fill=fill or self.ink,stroke_width=1 if bold else 0)
        box=d.textbbox((x,y),text,font=self.font(size),anchor=anchor,stroke_width=1 if bold else 0)
        self.boxes.append({'tag':tag,'text':text,'font_size':size,'bbox':list(box)})

    def single_size(self,text,width,preferred,minimum,tag):
        if '\n' in text or '\r' in text: raise LayoutError(f'{tag} must be one line: {text!r}')
        for size in range(int(preferred),int(minimum)-1,-1):
            if self.width(text,size,True)<=width: return size
        raise LayoutError(f'{tag} cannot fit at {minimum}px. Shorten the wording: {text!r}')

    def lines(self,text,width,size):
        lines=['']
        for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9_./:+%−-]*|\n|[^\S\n]+|.',str(text)):
            if token=='\n': lines.append(''); continue
            for p in (list(token) if self.width(token,size)>width else [token]):
                if self.width(lines[-1]+p,size)>width and lines[-1]: lines.append(p.lstrip())
                else: lines[-1]+=p
        return [x.rstrip() for x in lines]

    def rich_lines(self,body,width,size,emphasis=(),tags=()):
        terms=sorted({str(t) for t in [*emphasis,*tags] if str(t)},key=len,reverse=True)
        pattern='('+'|'.join(re.escape(t) for t in terms)+')' if terms else None
        pieces=re.split(pattern,str(body)) if pattern else [str(body)]
        # Group punctuation with its neighbour before wrapping. Styled tokens
        # remain separate inside a group so their baseline and highlight survive.
        close='，。、；：！？）】》〉」』〕］｝”’…,.!?;:%)]}'
        opening='（【《〈「『〔［｛“‘([{'
        paragraphs=[[]]
        def group_text(group):return ''.join(t[0] for t in group)
        for piece in pieces:
            if not piece: continue
            bold,tagged=piece in emphasis,piece in tags
            tokens=[piece] if tagged else re.findall(r'[A-Za-z0-9][A-Za-z0-9_./:+%−-]*|\n|[^\S\n]+|.',piece)
            for token in tokens:
                if token=='\n':paragraphs.append([]);continue
                tw=self.width(token,size,bold)+(8 if tagged else 0)
                if tw>width:
                    raise LayoutError(f'Body word or tag exceeds the line width; shorten it without splitting its spelling: {token!r}')
                item=(token,bold,tagged,tw);groups=paragraphs[-1]
                if token[0] in close and groups:
                    while len(groups)>1 and group_text(groups[-1]).isspace():
                        whitespace=groups.pop();groups[-1].extend(whitespace)
                    groups[-1].append(item)
                elif groups and group_text(groups[-1]).rstrip() and group_text(groups[-1]).rstrip()[-1] in opening:
                    groups[-1].append(item)
                else:groups.append([item])
        lines=[]
        for groups in paragraphs:
            wrapped=[[]];used=0.
            for group in groups:
                gw=sum(t[3] for t in group)
                if gw>width:
                    raise LayoutError(f'Body word with punctuation exceeds the line width; shorten the phrase: {group_text(group)!r}')
                if used+gw>width and wrapped[-1]:wrapped.append([]);used=0.
                if not wrapped[-1] and group_text(group).isspace():continue
                wrapped[-1].append(group);used+=gw
            # Avoid a final one/two-character Chinese orphan when whole groups
            # can be moved from the preceding line without either overflowing.
            if len(wrapped)>1:
                def short_tail():
                    value=''.join(group_text(g) for g in wrapped[-1])
                    visible=[c for c in value if not c.isspace() and c not in close+opening]
                    return len(visible)<=2 and any('\u3400'<=c<='\u9fff' for c in visible)
                while short_tail() and len(wrapped[-2])>1:
                    candidate=wrapped[-2][-1]
                    if sum(t[3] for g in [candidate,*wrapped[-1]] for t in g)>width:break
                    wrapped[-1].insert(0,wrapped[-2].pop())
            lines.extend([[t for group in line for t in group] for line in wrapped])
        return lines

    def rich_text(self,im,x,y,lines,size,tag):
        d=ImageDraw.Draw(im)
        baseline_offset=-self.font(size).getbbox('国',anchor='ls')[1]
        for line in lines:
            at=x
            for value,bold,tagged,width in line:
                if tagged:
                    d.rounded_rectangle((round(at),round(y-2),round(at+width),round(y+size+4)),radius=5,fill=self.v.get('tag_background','#EDF1F7'))
                self.text(im,(at+(4 if tagged else 0),y+baseline_offset),value,size,bold=bold,tag=tag,anchor='ls')
                at+=width
            y+=round(size*1.38)

    def icon(self,im,xy,value,color,size=38):
        x,y=xy;d=ImageDraw.Draw(im)
        def pt(a,b):return(round(x+a*size/40),round(y+b*size/40))
        def line(c):d.line([pt(a,b) for a,b in c],fill=color,width=3,joint='curve')
        def rect(a,b,c,e):d.rounded_rectangle((*pt(a,b),*pt(c,e)),radius=3,outline=color,width=3)
        name=str(value or 'file').lower().replace('icon:','')
        if any(k in name for k in ('chart','trend','timeline','activity')):
            line([(4,7),(4,35),(36,35)]);line([(9,26),(17,17),(24,23),(35,8)])
        elif any(k in name for k in ('clock','time','calendar')):
            d.ellipse((*pt(4,4),*pt(36,36)),outline=color,width=3);line([(20,9),(20,21),(29,26)])
        elif any(k in name for k in ('shield','lock','safe')):
            line([(20,3),(34,9),(32,24),(27,32),(20,37),(13,32),(8,24),(6,9),(20,3)]);line([(13,20),(18,25),(28,14)])
        elif any(k in name for k in ('code','terminal')):
            line([(13,8),(3,20),(13,32)]);line([(27,8),(37,20),(27,32)])
        elif any(k in name for k in ('user','people','person','team')):
            d.ellipse((*pt(13,3),*pt(27,17)),outline=color,width=3);d.arc((*pt(6,19),*pt(34,44)),180,360,fill=color,width=3);line([(6,33),(34,33)])
        elif any(k in name for k in ('cpu','chip','model')):
            rect(9,9,31,31);rect(15,15,25,25)
            for z in (14,26):
                line([(z,2),(z,8)]);line([(z,32),(z,38)]);line([(2,z),(8,z)]);line([(32,z),(38,z)])
        elif any(k in name for k in ('spark','star','release')):
            line([(20,2),(25,15),(38,20),(25,25),(20,38),(15,25),(2,20),(15,15),(20,2)])
        elif any(k in name for k in ('globe','world')):
            d.ellipse((*pt(3,3),*pt(37,37)),outline=color,width=3);d.ellipse((*pt(13,3),*pt(27,37)),outline=color,width=2);line([(4,20),(36,20)])
        elif any(k in name for k in ('check','done')):
            rect(3,3,37,37);line([(10,21),(17,28),(31,12)])
        elif any(k in name for k in ('dollar','price','money','credit')):
            rect(3,8,37,32);d.ellipse((*pt(15,13),*pt(25,27)),outline=color,width=2);line([(7,14),(11,14)]);line([(29,26),(33,26)])
        else:
            rect(5,3,35,37);line([(12,13),(27,13)]);line([(12,21),(28,21)]);line([(12,29),(23,29)])

    def card(self,im,box,title,lines,size,color,icon='file',tag='card'):
        x,y,right,bottom=box
        shadow=Image.new('RGBA',im.size);sd=ImageDraw.Draw(shadow)
        sd.rounded_rectangle((x,y+6,right,bottom+6),radius=self.radius,fill=(34,57,88,12))
        im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
        ImageDraw.Draw(im).rounded_rectangle(box,radius=self.radius,fill=self.card_bg,outline=self.border,width=1)
        pad=int(self.v.get('card_padding',28))
        fs=self.single_size(title,right-x-pad*2-50,self.v.get('card_title_size',42),32,tag+' title')
        self.icon(im,(x+pad,y+pad),icon,color)
        self.text(im,(x+pad+50,y+pad),title,fs,color,True,tag+' title')
        self.rich_text(im,x+pad,y+pad+61,lines,size,tag+' body')
        self.boxes.append({'tag':tag+' bounds','bbox':list(box)})

    def news(self,scene):
        cards=scene.get('visual',{}).get('cards',[])
        if not 1<=len(cards)<=6:raise LayoutError(f"{scene['id']}: news visual.cards needs 1–6 entries")
        count=len(cards);cols=1 if count==1 else 2 if count in (2,4) else 3
        cw=980 if count==1 else 740 if cols==2 else 556
        gap,pad=self.gap,int(self.v.get('card_padding',28))
        title=_head(scene)
        if not title:raise LayoutError(f"{scene['id']}: missing headline")
        fs=self.single_size(title,1740,self.v.get('headline_size',self.v.get('head',72)),self.v.get('headline_min_size',52),'headline')
        group=None
        for size in range(int(self.v.get('body_size',self.v.get('body',34))),int(self.v.get('body_min_size',30))-1,-1):
            rich=[]
            for c in cards:
                body=c.get('body','');body='\n'.join(map(str,body)) if isinstance(body,list) else body
                rich.append(self.rich_lines(body,cw-pad*2,size,c.get('emphasis',[]),c.get('tags',[])))
            heights=[pad*2+61+len(x)*round(size*1.38) for x in rich]
            rows=[max(heights[a:a+cols]) for a in range(0,count,cols)]
            height=fs+42+sum(rows)+gap*(len(rows)-1)
            if height<=902-(self.top+30):group=(rich,rows,height,size);break
        if group is None:raise LayoutError(f"{scene['id']}: cards exceed readable area at >=30px. Shorten repeated prose or split the news item.")
        rich,rows,height,size=group;im=Image.new('RGBA',(W,H),self.bg)
        top=round((self.top+30+902-height)/2)
        self.text(im,((W-self.width(title,fs,True))/2,top),title,fs,self.accent,True,'headline')
        y=top+fs+42
        for row,rh in enumerate(rows):
            start=row*cols;n=min(cols,count-start);left=round((W-(n*cw+(n-1)*gap))/2)
            for col in range(n):
                idx=start+col;x=left+col*(cw+gap)
                self.card(im,(x,y,x+cw,y+rh),str(cards[idx]['title']),rich[idx],size,self.colors[idx%len(self.colors)],cards[idx].get('icon','file'),f'card-{idx+1}')
            y+=rh+gap
        # Bounds are checked against each actual card, not just the full canvas.
        for j in range(count):
            prefix=f'card-{j+1}'
            bounds=next(b['bbox'] for b in self.boxes if b['tag']==prefix+' bounds')
            for b in self.boxes:
                if b['tag'] in (prefix+' title',prefix+' body'):
                    x1,y1,x2,y2=b['bbox']
                    if not(bounds[0]-2<=x1<x2<=bounds[2]+2 and bounds[1]-2<=y1<y2<=bounds[3]+2):
                        raise LayoutError(f"{scene['id']}: text escapes {prefix}: {b['text']!r}")
        return im

    def navigation(self,im,scenes,timeline,total,index):
        d=ImageDraw.Draw(im);d.rectangle((0,0,W,self.top),fill=self.nav_bg);d.rectangle((0,H-self.bottom,W,H),fill=self.nav_bg)
        groups=[]
        for i,(scene,seg) in enumerate(zip(scenes,timeline)):
            name=self.v.get('overview_nav_label','总览') if scene['layout']=='overview' else '结束' if scene['layout']=='closing' else scene['section']
            if groups and groups[-1]['name']==name and scene['layout']=='news':groups[-1]['end']=seg['end'];groups[-1]['last']=i
            else:groups.append({'name':name,'start':seg['start'],'end':seg['end'],'first':i,'last':i})
        for g in groups:
            x,right=W*g['start']/total,W*g['end']/total;active=g['first']<=index<=g['last']
            if active:d.rectangle((round(x),0,round(right),self.top),fill=self.accent)
            d.line((round(right),0,round(right),self.top),fill='#CDD8E7',width=1)
            fs=next((z for z in range(24,8,-1) if self.width(g['name'],z,active)<=right-x-5),9)
            # A short closing segment keeps its true duration even when its
            # label cannot fit at the minimum font size.
            if right-x>=12 and self.width(g['name'],fs,active)+5<=right-x:
                self.text(im,((x+right-self.width(g['name'],fs))/2,(self.top-fs)/2),g['name'],fs,'#FFFFFF' if active else self.ink,active,'navigation section')
        d.line((0,H-self.bottom,W,H-self.bottom),fill='#B8C9DF',width=2)
        for i,(scene,seg) in enumerate(zip(scenes,timeline)):
            x,right=W*seg['start']/total,W*seg['end']/total
            active=i==index
            label=self.v.get('overview_nav_label','总览') if scene['layout']=='overview' else '结束' if scene['layout']=='closing' else str(scene.get('nav_label') or scene['section'])
            if active:
                d.rectangle((round(x),H-self.bottom+2,round(right),H-self.progress_height-1),fill='#FFFFFF')
            d.line((round(right),H-self.bottom,round(right),H-self.progress_height),fill='#B8C9DF',width=2)
            preferred=int(self.v.get('nav_story_size',19));minimum=int(self.v.get('nav_story_min_size',9))
            if not 9<=minimum<=preferred<=28:
                raise LayoutError('Story navigation font sizes must satisfy 9 <= minimum <= preferred <= 28')
            fitted=None
            for fs in range(preferred,minimum-1,-1):
                lines=self.lines(label,max(1,right-x-8),fs)
                if (len(lines)<=2 and len(lines)*(fs+2)<=self.bottom-self.progress_height-4
                        and all(self.width(line,fs,active)<=right-x-8 for line in lines)):
                    fitted=(fs,lines);break
            if fitted:
                fs,lines=fitted
                y=H-self.bottom+(self.bottom-len(lines)*(fs+2)-self.progress_height)/2
                for text in lines:
                    self.text(im,((x+right-self.width(text,fs,active))/2,y),text,fs,self.accent if active else self.ink,active,'navigation story');y+=fs+2
            elif index==0:
                self.navigation_notes.append({'id':scene['id'],'label':label,'width':round(right-x,2),
                    'status':'label-does-not-fit','action':'Shorten the story label or review the brief segment; duration proportions are preserved.'})

    def footer(self,scene,episode):
        sources={s['id']:s for s in episode.get('sources',[]) if isinstance(s,dict) and 'id' in s}
        label=str(scene.get('source_label','')).strip()
        if not label:
            names=[]
            for sid in scene.get('source_ids',[]):
                s=sources.get(sid,{});name=s.get('publisher') or s.get('label') or s.get('title') or sid
                if name not in names:names.append(str(name))
            label=' / '.join(names) if names else '本期资料来源见来源清单'
        if not label.startswith('来源'):label='来源：'+label
        size=self.single_size(label,1480,self.v.get('source_size',20),16,'source attribution')
        im=Image.new('RGBA',(W,32));d=ImageDraw.Draw(im)
        d.rounded_rectangle((64,0,1856,30),radius=6,fill=_rgb(self.bg,240))
        self.text(im,(76,6),label,size,self.muted,tag='source attribution')
        disclosure='AI 配音 · 资讯整理'
        self.text(im,(W-78-self.width(disclosure,19),6),disclosure,19,self.muted,tag='disclosure')
        return im

    def overview(self,episode,news,duration,work,index):
        im=Image.new('RGBA',(W,H),self.bg);day=date.fromisoformat(episode['date']);title=f'{day.isoformat()} 资讯概览'
        self.text(im,((W-self.width(title,70,True))/2,92),title,70,self.accent,True,'overview date')
        groups=OrderedDict()
        for s in news:groups.setdefault(s['section'],[]).append(_head(s))
        cw,gap,vh=784,32,684;sections=[];heights=[0,0]
        for section,items in groups.items():
            lines=[self.lines('• '+item,cw-48,29) for item in items]
            height=28+48+sum(len(x)*40+6 for x in lines)+20;col=0 if heights[0]<=heights[1] else 1
            sections.append((col,heights[col],section,lines,height));heights[col]+=height+24
        sh=max(vh,max(heights)-24);strip=Image.new('RGBA',(1600,sh),self.bg);d=ImageDraw.Draw(strip)
        for col,y,section,items,height in sections:
            x=col*(cw+gap);d.rounded_rectangle((x,y,x+cw,y+height),radius=24,fill=self.card_bg,outline='#E2E8F0',width=1)
            self.icon(strip,(x+24,y+24),'file',self.accent,34);self.text(strip,(x+70,y+24),section,36,self.accent,True,'overview section');yy=y+80
            for lines in items:
                for line in lines:self.text(strip,(x+24,yy),line,29,tag='overview bullet');yy+=40
                yy+=6
        path=work/f'overview-strip-{index:02}.png';strip.save(path)
        gradient=Image.new('RGBA',(1600,vh));gd=ImageDraw.Draw(gradient)
        for z in range(38):
            alpha=round(255*(1-z/38));gd.line((0,z,1600,z),fill=_rgb(self.bg,alpha));gd.line((0,vh-1-z,1600,vh-1-z),fill=_rgb(self.bg,alpha))
        gp=work/f'overview-gradient-{index:02}.png';gradient.save(gp)
        overflow=max(0,sh-vh);first=round(overflow*.8);a,b,c,e=[duration*f for f in (.36,.48,.74,.80)]
        expr=(f'if(lt(t,{a:.6f}),0,if(lt(t,{b:.6f}),{first}*(t-{a:.6f})/{b-a:.6f},'
              f'if(lt(t,{c:.6f}),{first},if(lt(t,{e:.6f}),{first}+{overflow-first}*(t-{c:.6f})/{e-c:.6f},{overflow}))))')
        return im,{'path':path,'gradient':gp,'width':1600,'height':vh,'x':160,'y':196,'expression':expr,
                   'overflow':overflow,'first_shift':first,'phase_times':[a,b,c,e]}

def _caption_assets(audio,layout,work):
    caps,srt,prev=[],[],-1.;size=int(layout.v.get('subtitle_size',46))
    if size<42:raise LayoutError('Landscape subtitles must remain at least 42px')
    for i,cap in enumerate(audio.get('captions',[])):
        start,end=float(cap['start']),float(cap['end'])
        if not 0<=start<end<=float(audio['duration'])+.002 or start<prev-.011:raise ValueError(f'Invalid/overlapping subtitle {i}: {start}, {end}')
        prev=end;text=str(cap['text']).strip()
        if not text or '\n' in text or '\r' in text or layout.width(text,size)>1780:
            raise LayoutError(f'Subtitle {i+1} is not a readable single line at {size}px. Split a complete semantic phrase using measured audio boundaries; do not silently shrink, truncate, or invent timing: {text!r}')
        tw=math.ceil(layout.width(text,size));height=size+22;im=Image.new('RGBA',(tw+32,height));d=ImageDraw.Draw(im)
        opacity=float(layout.v.get('subtitle_opacity',.76))
        if not 0<=opacity<=1:raise LayoutError('subtitle_opacity must be between 0 and 1')
        d.rounded_rectangle((0,0,im.width-1,im.height-1),radius=7,fill=_rgb(layout.v.get('subtitle_background','#626975'),round(opacity*255)))
        layout.text(im,(16,10),text,size,layout.v.get('subtitle_color','#FFFFFF'),tag='subtitle');path=work/f'caption-{i:04}.png';im.save(path)
        margin=int(layout.v.get('subtitle_margin_v',70))
        if margin<layout.bottom+6 or H-margin-height<940:raise LayoutError('Subtitle margin must keep the whole subtitle between the source footer and bottom navigation')
        caps.append({**cap,'start':start,'end':end,'text':text,'path':path,'x':(W-im.width)//2,'y':H-margin-height})
        srt.append(f'{i+1}\n{_stamp(start)} --> {_stamp(end)}\n{text}')
    if not caps:raise ValueError('No captions supplied')
    path=work/'字幕.srt';path.write_text('\n\n'.join(srt)+'\n',encoding='utf-8');return caps,path

def _media(scene,seg,captions,root,work,cache):
    duration=float(seg['end'])-float(seg['start']);records=[]
    for i,m in enumerate(scene.get('media',[])):
        m=dict(m);typ=m.get('type')
        if typ not in ('image','video') or m.get('fit','contain')!='contain':raise ValueError("Media must be image/video with fit='contain'")
        path=_resolve(root,m['path']).resolve()
        if not path.is_file():raise FileNotFoundError(f'Local media missing: {path}')
        if 'start' not in m:
            cue=str(m.get('cue','')).strip();matches=[c for c in captions if c['start']>=seg['start']-.002 and c['start']<seg['end'] and cue and cue in c['text']]
            if len(matches)!=1:raise ValueError(f"Media {m.get('id',i)} cue needs exactly one caption match")
            m['start']=matches[0]['start']-seg['start']
        start,end=float(m['start']),float(m.get('end',duration-.25));trim=float(m.get('trim_start',0))
        if not 0<=start<end<=duration+.002 or trim<0:raise ValueError(f"Media {m.get('id',i)} interval/trim outside scene")
        key=str(path)
        if key not in cache:
            item={'path':key,'sha256':_sha(path),'type':typ}
            if typ=='video':
                reader=imageio_ffmpeg.read_frames(key)
                try:meta=next(reader)
                finally:reader.close()
                item.update({'source_dimensions':list(meta['source_size']),'source_duration':meta.get('duration',0),'source_fps':meta.get('fps')})
            else:
                with Image.open(path) as im:item['source_dimensions']=list(ImageOps.exif_transpose(im).size)
            cache[key]=item
        item=cache[key]
        if typ=='video' and (not item.get('source_duration') or trim+end-start>item['source_duration']+.05):raise ValueError(f'Video {path.name} shorter than trim/window; no freeze-frame fallback')
        records.append({**item,'id':m.get('id',f"{scene['id']}-media-{i+1}"),'scene_id':scene['id'],'start':start,'end':end,
                        'trim_start':trim,'fit':'contain','source_ids':m.get('source_ids',[]),'claim_ids':m.get('claim_ids',[]),'audio':'muted' if typ=='video' else 'none'})
    return sorted(records,key=lambda m:m['start'])

def _merged_windows(media):
    windows=[]
    for m in media:
        if windows and m['start']<=windows[-1][1]+.001:windows[-1][1]=max(windows[-1][1],m['end'])
        else:windows.append([m['start'],m['end']])
    return windows

def _encode_scene(ff,index,page,overview,footer,prev,scene,seg,caps,media,frames,duration,layout,work,profile,fixture_label):
    args,graph=['-y'],[];inputs=0
    page_fade=float(layout.v.get('page_fade_seconds',.25))
    media_fade=float(layout.v.get('media_fade_seconds',.30))
    media_w=int(layout.v.get('media_max_width',1600));media_h=int(layout.v.get('media_max_height',900))
    dim=float(layout.v.get('media_dim_opacity',.14))
    if not 0<page_fade<=1 or not 0<media_fade<=1 or not 0<=dim<=.5:raise LayoutError('Transition duration or media mask opacity outside safe rendering range')
    if not 1<=media_w<=W or not 1<=media_h<=H-layout.top-layout.bottom:raise LayoutError('Media containment must fit between the navigation bars')
    def image_input(path):
        nonlocal inputs
        at=inputs;inputs+=1;args.extend(['-loop','1','-framerate',FPS,'-i',str(path)]);return at
    def overlay(base,fg,x=0,y=0,enable=None):
        out=f'v{len(graph)}';rule=f":enable='{enable}'" if enable else ''
        graph.append(f"[{base}][{fg}]overlay=x='{x}':y='{y}':eof_action=pass:shortest=0{rule}[{out}]");return out
    image_input(page);graph.append('[0:v]format=rgba,setpts=PTS-STARTPTS[base]');current='base'
    if overview:
        at=image_input(overview['path']);graph.append(f"[{at}:v]crop={overview['width']}:{overview['height']}:0:'{overview['expression']}',format=rgba[overview]")
        current=overlay(current,'overview',overview['x'],overview['y']);at=image_input(overview['gradient']);current=overlay(current,f'{at}:v',overview['x'],overview['y'])
    maskpath=work/'body-mask.png'
    im=Image.new('RGBA',(W,H));ImageDraw.Draw(im).rectangle((0,layout.top,W,H-layout.bottom-1),fill=(0,0,0,round(dim*255)));im.save(maskpath)
    for j,(start,end) in enumerate(_merged_windows(media)):
        at=image_input(maskpath);fade=min(media_fade,(end-start)/2);tag=f'mask{j}'
        graph.append(f'[{at}:v]format=rgba,fade=t=in:st=0:d={fade:.6f}:alpha=1,fade=t=out:st={end-start-fade:.6f}:d={fade:.6f}:alpha=1,setpts=PTS-STARTPTS+{start:.6f}/TB[{tag}]')
        current=overlay(current,tag,enable=f'gte(t,{start:.6f})*lt(t,{end:.6f})')
    for j,m in enumerate(media):
        if m['type']=='image':at=image_input(m['path'])
        else:
            at=inputs;inputs+=1;args.extend(['-ss',m['trim_start'],'-t',m['end']-m['start'],'-i',m['path']])
        length=m['end']-m['start'];fade=min(media_fade,length/2);tag=f'media{j}'
        graph.append(f"[{at}:v]fps={FPS},scale={media_w}:{media_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,format=rgba,setsar=1,setpts=PTS-STARTPTS,fade=t=in:st=0:d={fade:.6f}:alpha=1,fade=t=out:st={length-fade:.6f}:d={fade:.6f}:alpha=1,setpts=PTS+{m['start']:.6f}/TB[{tag}]")
        current=overlay(current,tag,'(W-w)/2',f'{(layout.top+H-layout.bottom)/2:.3f}-h/2',f"gte(t,{m['start']:.6f})*lt(t,{m['end']:.6f})")
    at=image_input(footer);current=overlay(current,f'{at}:v',0,906)
    if prev:
        at=image_input(prev);graph.append(f'[{at}:v]format=rgba,fade=t=out:st=0:d={page_fade:.6f}:alpha=1[previous]');current=overlay(current,'previous',enable=f'lt(t,{page_fade:.6f})')
    for cap in caps:
        start=max(0,cap['start']-seg['start']);end=min(seg['end']-seg['start'],cap['end']-seg['start'])
        if end<=start:continue
        at=image_input(cap['path']);current=overlay(current,f'{at}:v',cap['x'],cap['y'],f'gte(t,{start:.6f})*lt(t,{end:.6f})')
    progress=work/'progress.png'
    Image.new('RGBA',(W,layout.progress_height),layout.accent).save(progress)
    at=image_input(progress);current=overlay(current,f'{at}:v',f"-{W}+{W}*({seg['start']:.6f}+t)/{duration:.9f}",H-layout.progress_height)
    if fixture_label:
        path=work/'technical-fixture.png'
        im=Image.new('RGBA',(W,36));ImageDraw.Draw(im).rectangle((0,0,W,36),fill=(160,36,46,225))
        text=str(fixture_label);fs=layout.single_size(text,1700,24,22,'fixture watermark')
        layout.text(im,((W-layout.width(text,fs))/2,6),text,fs,'#FFFFFF',True);im.save(path)
        at=image_input(path);current=overlay(current,f'{at}:v',0,layout.top)
    graph.append(f'[{current}]format=yuv420p[out]');script=work/f'filters-{index:02}.txt';script.write_text(';\n'.join(graph),encoding='utf-8')
    part=work/f'part-{index:02}.mp4'
    args.extend(['-filter_complex_threads','1','-filter_complex_script',str(script),'-map','[out]','-an','-frames:v',frames,'-r',FPS,'-c:v','libx264','-threads','4','-preset',profile.get('video',{}).get('preset','fast'),'-crf',profile.get('video',{}).get('crf',19),'-pix_fmt','yuv420p',str(part)])
    _run(ff,args,work)
    last=work/f'last-{index:02}.png';_run(ff,['-y','-ss',f'{max(0,(frames-1)/FPS):.6f}','-i',part,'-frames:v','1',last],work)
    with Image.open(last) as im:
        im=im.convert('RGBA');d=ImageDraw.Draw(im)
        d.rectangle((0,940,W,H-layout.bottom-1),fill=(0,0,0,0));d.rectangle((0,H-layout.progress_height,W,H),fill=(0,0,0,0));im.save(last)
    return part,last

def render_video(episode,audio,runtime_root,work,profile):
    started=time.perf_counter();root,work=Path(runtime_root),Path(work);work.mkdir(parents=True,exist_ok=True)
    settings=profile.get('video',{})
    if (int(settings.get('width',W)),int(settings.get('height',H)),int(settings.get('fps',FPS)))!=(W,H,FPS):raise ValueError('Schema 2 requires landscape 1920×1080 at 30fps')
    duration=float(audio['duration'])
    if not float(settings.get('min_seconds',1))<=duration<=float(settings.get('max_seconds',900)):raise ValueError(f'Narration length {duration:.2f}s outside profile limits')
    scenes,timeline=episode['scenes'],audio['timeline']
    if not scenes or len(scenes)!=len(timeline):raise ValueError('Scene/timeline counts differ')
    if scenes[0]['layout']!='overview' or scenes[-1]['layout']!='closing':raise ValueError('Schema 2 begins with overview and ends with closing')
    if len(scenes)<3 or any(s['layout']!='news' for s in scenes[1:-1]):raise ValueError('Overview/closing must enclose at least one news scene')
    previous,bounds=0.,[0]
    for scene,seg in zip(scenes,timeline):
        if scene['id']!=seg['id'] or abs(float(seg['start'])-previous)>.002:raise ValueError('Timeline IDs/order/gaps mismatch')
        previous=float(seg['end']);bounds.append(round(previous*FPS))
    if abs(previous-duration)>.002 or any(b<=a for a,b in zip(bounds,bounds[1:])):raise ValueError('Invalid timeline duration/boundaries')
    layout=Layout(root,profile);caps,srt=_caption_assets(audio,layout,work);ff=imageio_ffmpeg.get_ffmpeg_exe()
    pages,parts,boxes,overviews,assets,cache=[],[],[],[],[],{}
    last,last_news,last_scene=None,None,None
    for i,(scene,seg) in enumerate(zip(scenes,timeline)):
        layout.boxes=[];overview=None
        if scene['layout']=='overview':
            im,overview=layout.overview(episode,scenes[1:-1],seg['end']-seg['start'],work,i)
            overviews.append({k:str(v) if isinstance(v,Path) else v for k,v in overview.items()});footer_scene=scene
        elif scene['layout']=='news':
            im=layout.news(scene);last_news,last_scene=im.copy(),scene;footer_scene=scene
        else:im=last_news.copy();footer_scene=last_scene
        layout.navigation(im,scenes,timeline,duration,i);footer=layout.footer(footer_scene,episode);im.alpha_composite(footer,(0,906))
        page=work/f'page-{i:02}.png';im.convert('RGB').save(page);pages.append(page)
        fp=work/f'footer-{i:02}.png';footer.save(fp);boxes.append({'id':scene['id'],'boxes':layout.boxes})
        media=_media(scene,seg,caps,root,work,cache) if scene['layout']=='news' else [];assets.extend(media)
        eligible=[c for c in caps if c['end']>seg['start'] and c['start']<seg['end']]
        fixture_label=episode.get('fixture_label') or ('本地模板验证 · 非现实新闻' if episode.get('fixture') else None)
        part,last=_encode_scene(ff,i,page,overview,fp,last,scene,seg,eligible,media,bounds[i+1]-bounds[i],duration,layout,work,profile,fixture_label);parts.append(part)
    (work/'concat.txt').write_text('\n'.join(f"file '{p.name}'" for p in parts),encoding='utf-8')
    final=work/f"AI{profile.get('delivery',{}).get('bulletin_label','早报')}_{episode['date']}_蓝色横屏版.mp4";mix=_resolve(root,audio['paths']['mix'])
    _run(ff,['-y','-f','concat','-safe','0','-i','concat.txt','-i',mix,'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','256k','-t',f'{duration:.9f}','-movflags','+faststart','-metadata','comment=AI narration and editorial graphics; source video audio muted',final.name],work)
    decoded=_run(ff,['-i',final.name,'-progress','pipe:1','-f','null','-'],work);counts=re.findall(r'(?m)^frame=(\d+)$',decoded.stdout)
    if not counts or int(counts[-1])!=bounds[-1]:raise ValueError('Decoded frame count differs from timeline')
    frames=[]
    for i,seg in enumerate(timeline):
        eligible=[c for c in caps if c['start']>=seg['start']-.002 and c['end']<=seg['end']+.002]
        cap=max(eligible,key=lambda c:len(c['text'])) if eligible else None
        t=(cap['start']+cap['end'])/2 if cap else (seg['start']+seg['end'])/2;frame=work/f'qa-{i:02}.png'
        _run(ff,['-y','-ss',f'{t:.6f}','-i',final.name,'-frames:v','1',frame.name],work)
        with Image.open(frame) as im:
            if im.size!=(W,H):raise ValueError('Decoded dimensions invalid')
        frames.append(str(frame))
    cols=min(3,len(frames));rows=math.ceil(len(frames)/cols);contact=Image.new('RGB',(cols*640,rows*360),layout.bg)
    for i,path in enumerate(frames):
        with Image.open(path) as im:contact.paste(im.resize((640,360)),((i%cols)*640,(i//cols)*360))
    contact_path=work/'contact.jpg';contact.save(contact_path,quality=93);cover=work/'封面.png'
    _run(ff,['-y','-ss',f"{min(timeline[0]['end']*.25,2):.6f}",'-i',final.name,'-frames:v','1',cover.name],work)
    metrics={'duration':duration,'video_frame_duration':bounds[-1]/FPS,'duration_difference_seconds':abs(bounds[-1]/FPS-duration),'width':W,'height':H,'fps':FPS,'frame_count':int(counts[-1]),'scene_frame_boundaries':bounds,'scene_count':len(scenes),'caption_count':len(caps),'caption_alignment':audio.get('caption_alignment','provided audio boundaries'),'layout_bounds':'passed','decode':'passed','visual_review':'pending_agent_inspection','sha256':_sha(final),'seconds_to_render':time.perf_counter()-started,'schema_version':2,'page_transition_seconds':float(layout.v.get('page_fade_seconds',.25)),'media_assets':assets,'overview_scroll':overviews,'fixture_label':fixture_label,
             'effective_visual':dict(layout.v),'navigation_notes':layout.navigation_notes}
    for name,data in [('layout-boxes.json',boxes),('media-assets.json',assets),('video-qa.json',metrics)]:
        (work/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'video_path':str(final),'cover_path':str(cover),'srt_path':str(srt),'contact_path':str(contact_path),'frames':frames,'metrics':metrics}
