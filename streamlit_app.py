import streamlit as st
import subprocess
import os
import re
import shutil
import tempfile
import json
import time
import zipfile
import io
from pathlib import Path

st.set_page_config(
    page_title="🎬 Video & Subtitle Merger",
    page_icon="🎬",
    layout="wide"
)

# ===========================================================
# SETTINGS
# ===========================================================

# ── Subtitle position ──────────────────────────────────────
# Increase chinese_margin_v to move subtitles UP
# e.g. 0.18 = 18% from bottom,  0.25 = 25% from bottom
CHINESE_MARGIN_V_PCT = 0.18
GAP_PCT              = 0.015
CHINESE_FONT_PCT     = 0.052
ENGLISH_FONT_PCT     = 0.038

# ===========================================================
# CSS
# ===========================================================
st.markdown("""
<style>
  #MainMenu,footer,header{visibility:hidden}
  .stApp{background:linear-gradient(135deg,#0f0c29,#302b63,#24243e)}
  .block-container{padding-top:1.5rem}
  div[data-testid="stExpander"]{
    background:rgba(255,255,255,.05);
    border:1px solid rgba(255,255,255,.1);
    border-radius:12px;
  }
  .stButton>button{
    background:linear-gradient(135deg,#6366f1,#8b5cf6);
    color:#fff;border:none;border-radius:10px;
    font-weight:700;letter-spacing:.5px;
  }
  .stButton>button:hover{
    transform:translateY(-1px);
    box-shadow:0 8px 20px rgba(99,102,241,.4);
  }
  .stDownloadButton>button{
    background:linear-gradient(135deg,#10b981,#059669)!important;
    color:#fff!important;border:none!important;border-radius:10px!important;
    font-weight:700!important;
  }
</style>
""", unsafe_allow_html=True)


# ===========================================================
# FFMPEG CHECKS
# ===========================================================

@st.cache_data(ttl=60)
def check_ffmpeg():
    try:
        r = subprocess.run(['ffmpeg','-version'],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False

@st.cache_data(ttl=60)
def check_filters():
    try:
        r = subprocess.run(['ffmpeg','-filters'],
                           capture_output=True, text=True, timeout=10)
        out = r.stdout
        return {
            'subtitles': any('subtitles' in l and 'V->V' in l for l in out.split('\n')),
            'ass':       any(' ass '    in l and 'V->V' in l for l in out.split('\n')),
        }
    except Exception:
        return {'subtitles': False, 'ass': False}


# ===========================================================
# SRT UTILITIES
# ===========================================================

def parse_srt(path):
    encodings = ['utf-8-sig','utf-8','latin-1','cp1252']
    content = None
    for enc in encodings:
        try:
            with open(path,'r',encoding=enc) as f:
                content = f.read()
            break
        except Exception:
            continue
    if not content:
        with open(path,'r',encoding='utf-8',errors='replace') as f:
            content = f.read()

    content = content.replace('\ufeff','').replace('\r\n','\n').replace('\r','\n')
    entries = []

    for block in re.split(r'\n\s*\n', content.strip()):
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        time_match = None
        time_idx   = -1
        for li, line in enumerate(lines):
            m = re.match(
                r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*'
                r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})', line.strip()
            )
            if m:
                time_match = m
                time_idx   = li
                break
        if not time_match:
            continue
        g     = time_match.groups()
        start = int(g[0])*3600 + int(g[1])*60 + int(g[2]) + int(g[3])/1000
        end   = int(g[4])*3600 + int(g[5])*60 + int(g[6]) + int(g[7])/1000
        text  = '\n'.join(lines[time_idx+1:])
        text  = re.sub(r'<[^>]+>','',text)
        text  = re.sub(r'\{[^}]+\}','',text).strip()
        if text:
            entries.append({'start':start,'end':end,'text':text})
    return entries


def detect_language(text):
    cjk, total = 0, 0
    for ch in text:
        cp     = ord(ch)
        total += 1
        if (0x4E00<=cp<=0x9FFF or 0x3400<=cp<=0x4DBF or
            0xF900<=cp<=0xFAFF or 0x2E80<=cp<=0x2EFF or
            0x3000<=cp<=0x303F or 0xFF00<=cp<=0xFFEF or
            0xAC00<=cp<=0xD7AF or 0x3040<=cp<=0x309F or
            0x30A0<=cp<=0x30FF):
            cjk += 1
    if total == 0:
        return 'latin'
    return 'cjk' if cjk/total > 0.2 else 'latin'


def split_cjk_latin(text):
    cjk_lines, lat_lines = [], []
    for line in [l.strip() for l in text.split('\n') if l.strip()]:
        (cjk_lines if detect_language(line)=='cjk' else lat_lines).append(line)
    return '\n'.join(cjk_lines), '\n'.join(lat_lines)


def format_ass_time(s):
    h=int(s//3600); m=int((s%3600)//60); sc=int(s%60); cs=int((s%1)*100)
    return f"{h}:{m:02d}:{sc:02d}.{cs:02d}"

def format_srt_time(s):
    h=int(s//3600); m=int((s%3600)//60); sc=int(s%60); ms=int((s%1)*1000)
    return f"{h:02d}:{m:02d}:{sc:02d},{ms:03d}"


def create_ass(srt_path, ass_path, w=1920, h=1080):
    """
    Dual-style ASS:
      Chinese — sits chinese_margin_v from bottom
      English — sits above Chinese
    """
    entries = parse_srt(srt_path)

    cjk_fs  = max(int(h * CHINESE_FONT_PCT), 26)
    lat_fs  = max(int(h * ENGLISH_FONT_PCT), 20)
    mlr     = int(w * 0.06)
    cjk_mv  = int(h * CHINESE_MARGIN_V_PCT)
    eng_mv  = cjk_mv + cjk_fs + int(h * GAP_PCT)

    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Chinese,Arial Unicode MS,{cjk_fs},"
        f"&H00FFFFFF,&H000000FF,&H00000000,&H96000000,"
        f"-1,0,0,0,100,100,0,0,1,3,1,2,{mlr},{mlr},{cjk_mv},1\n"
        f"Style: English,Arial,{lat_fs},"
        f"&H00FFFFFF,&H000000FF,&H00000000,&H96000000,"
        f"0,0,0,0,100,100,0,0,1,2,1,2,{mlr},{mlr},{eng_mv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, "
        "MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dlg = []
    for e in entries:
        s   = format_ass_time(e['start'])
        en  = format_ass_time(e['end'])
        cjk, lat = split_cjk_latin(e['text'])

        if cjk and lat:
            dlg.append(f"Dialogue: 0,{s},{en},Chinese,,0,0,0,,{cjk.replace(chr(10),'\\N')}\n")
            dlg.append(f"Dialogue: 0,{s},{en},English,,0,0,0,,{lat.replace(chr(10),'\\N')}\n")
        elif cjk:
            dlg.append(f"Dialogue: 0,{s},{en},Chinese,,0,0,0,,{cjk.replace(chr(10),'\\N')}\n")
        else:
            fb = (lat or e['text']).replace('\n','\\N')
            dlg.append(f"Dialogue: 0,{s},{en},English,,0,0,0,,{fb}\n")

    with open(ass_path,'w',encoding='utf-8') as f:
        f.write(header)
        f.writelines(dlg)

    return len(entries)


def clean_srt(src, dst):
    entries = parse_srt(src)
    with open(dst,'w',encoding='utf-8') as f:
        for i,e in enumerate(entries,1):
            f.write(f"{i}\n{format_srt_time(e['start'])} --> "
                    f"{format_srt_time(e['end'])}\n{e['text']}\n\n")
    return len(entries)


def get_video_info(path):
    try:
        r = subprocess.run(
            ['ffprobe','-v','quiet','-print_format','json',
             '-show_streams','-show_format', path],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            w,h,dur = 1920,1080,0
            for s in data.get('streams',[]):
                if s.get('codec_type')=='video':
                    w   = int(s.get('width',  1920))
                    h   = int(s.get('height', 1080))
                    dur = float(s.get('duration',0))
            if dur==0:
                dur = float(data.get('format',{}).get('duration',0))
            return {'width':w,'height':h,'duration':dur}
    except Exception:
        pass
    return {'width':1920,'height':1080,'duration':0}


# ===========================================================
# FFMPEG RUNNERS
# ===========================================================

def run_ff(cmd, timeout=7200):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def good(result, path):
    return (result and result.returncode==0
            and os.path.exists(path)
            and os.path.getsize(path) > 10000)


# ── hard sub methods ──────────────────────────────────────

def burn_ass(video, sub, out, sub_ext, work_dir, info):
    ass = os.path.join(work_dir,'styled.ass')
    if create_ass(os.path.abspath(sub), ass, info['width'], info['height']) == 0:
        return None
    esc = os.path.abspath(ass).replace('\\','/').replace(':','\\:')
    return run_ff([
        'ffmpeg','-y','-i', os.path.abspath(video),
        '-vf', f"ass='{esc}'",
        '-c:v','libx264','-crf','20','-preset','fast',
        '-c:a','aac','-b:a','192k','-movflags','+faststart',
        os.path.abspath(out)
    ])


def burn_subtitles(video, sub, out, sub_ext, work_dir, info):
    abs_sub = os.path.abspath(sub)
    esc     = abs_sub.replace('\\','/').replace(':','\\:')
    vf      = f"ass='{esc}'" if sub_ext in ('.ass','.ssa') else f"subtitles='{esc}'"
    return run_ff([
        'ffmpeg','-y','-i', os.path.abspath(video),
        '-vf', vf,
        '-c:v','libx264','-crf','20','-preset','fast',
        '-c:a','aac','-b:a','192k','-movflags','+faststart',
        os.path.abspath(out)
    ])


def burn_ffmpeg_conv(video, sub, out, sub_ext, work_dir, info):
    conv = os.path.join(work_dir,'conv.ass')
    r    = run_ff(['ffmpeg','-y','-i', os.path.abspath(sub), conv], timeout=60)
    if not (r and r.returncode==0 and os.path.exists(conv)):
        return None
    esc = os.path.abspath(conv).replace('\\','/').replace(':','\\:')
    return run_ff([
        'ffmpeg','-y','-i', os.path.abspath(video),
        '-vf', f"ass='{esc}'",
        '-c:v','libx264','-crf','20','-preset','fast',
        '-c:a','aac','-b:a','192k','-movflags','+faststart',
        os.path.abspath(out)
    ])


# ── soft sub methods ──────────────────────────────────────

def soft_mkv(video, sub, out, sub_ext):
    mkv = str(Path(out).with_suffix('.mkv'))
    sc  = 'ass' if sub_ext in ('.ass','.ssa') else 'srt'
    r   = run_ff([
        'ffmpeg','-y',
        '-i', os.path.abspath(video),
        '-i', os.path.abspath(sub),
        '-map','0:v','-map','0:a?','-map','1:0',
        '-c:v','copy','-c:a','copy','-c:s', sc,
        '-metadata:s:s:0','language=eng',
        '-disposition:s:0','default', mkv
    ], timeout=3600)
    return r, mkv


def soft_mp4(video, sub, out, sub_ext, work_dir):
    clean = os.path.join(work_dir,'clean.srt')
    clean_srt(os.path.abspath(sub), clean)
    return run_ff([
        'ffmpeg','-y',
        '-i', os.path.abspath(video),
        '-i', clean,
        '-map','0:v','-map','0:a?','-map','1:0',
        '-c:v','copy','-c:a','copy','-c:s','mov_text',
        '-metadata:s:s:0','language=eng',
        '-disposition:s:0','default',
        '-movflags','+faststart',
        os.path.abspath(out)
    ], timeout=3600)


# ===========================================================
# CORE PROCESS
# ===========================================================

def process_episode(video_bytes, video_name, srt_bytes, srt_name,
                    ep_name, merge_type, log_fn=None):
    """
    Returns dict:
        success      : bool
        output_bytes : bytes | None
        filename     : str
        message      : str
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    job_dir = tempfile.mkdtemp(prefix='st_merger_')
    try:
        v_ext = Path(video_name).suffix.lower() or '.mp4'
        s_ext = Path(srt_name).suffix.lower()   or '.srt'
        v_path = os.path.join(job_dir, f'video{v_ext}')
        s_path = os.path.join(job_dir, f'subs{s_ext}')

        with open(v_path,'wb') as f: f.write(video_bytes)
        with open(s_path,'wb') as f: f.write(srt_bytes)

        if os.path.getsize(v_path) < 1000:
            return {'success':False,'output_bytes':None,
                    'filename':None,'message':'Video file too small / corrupt'}
        if os.path.getsize(s_path) < 5:
            return {'success':False,'output_bytes':None,
                    'filename':None,'message':'Subtitle file too small / corrupt'}

        entries = parse_srt(s_path)
        if not entries:
            return {'success':False,'output_bytes':None,
                    'filename':None,'message':'No subtitle entries found'}

        log(f"📊 {len(entries)} subtitle entries")

        info  = get_video_info(v_path)
        log(f"🎬 {info['width']}×{info['height']}  {info['duration']:.0f}s")

        safe = re.sub(r'[<>:"/\\|?*]','_', ep_name)
        safe = re.sub(r'_+','_', safe).strip('_') or 'episode'

        filters = check_filters()

        # ── HARD ──────────────────────────────────────────
        if merge_type == 'hard':
            out_path = os.path.join(job_dir, f'{safe}.mp4')
            out_name = f'{safe}.mp4'

            methods = []
            if filters.get('ass') or filters.get('subtitles'):
                methods += [
                    ('Dual-style ASS burn', burn_ass),
                    ('Subtitles filter',    burn_subtitles),
                    ('FFmpeg ASS convert',  burn_ffmpeg_conv),
                ]

            success, last_err = False, 'No filters available'
            for mname, mfn in methods:
                log(f"🔄 Trying: {mname}…")
                try:
                    result = mfn(v_path, s_path, out_path, s_ext, job_dir, info)
                    if good(result, out_path):
                        log(f"✅ {mname} succeeded!")
                        success = True
                        break
                    last_err = (result.stderr[-200:]
                                if result and result.stderr else 'failed')
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except Exception as ex:
                    last_err = str(ex)
                    log(f"❌ {mname}: {ex}")

            if not success:
                return {'success':False,'output_bytes':None,'filename':None,
                        'message':f'All methods failed: {last_err[:200]}'}

        # ── SOFT ──────────────────────────────────────────
        else:
            out_path = None
            out_name = None
            success  = False
            last_err = ''

            log("🔄 Soft sub → MKV…")
            try:
                base    = os.path.join(job_dir, f'{safe}.mp4')
                r, mkv  = soft_mkv(v_path, s_path, base, s_ext)
                if good(r, mkv):
                    out_path = mkv
                    out_name = f'{safe}.mkv'
                    success  = True
                    log("✅ MKV soft sub succeeded!")
                else:
                    last_err = r.stderr[-200:] if r and r.stderr else 'mkv failed'
            except Exception as ex:
                last_err = str(ex)

            if not success:
                log("🔄 Soft sub → MP4…")
                try:
                    mp4 = os.path.join(job_dir, f'{safe}.mp4')
                    r   = soft_mp4(v_path, s_path, mp4, s_ext, job_dir)
                    if good(r, mp4):
                        out_path = mp4
                        out_name = f'{safe}.mp4'
                        success  = True
                        log("✅ MP4 soft sub succeeded!")
                    else:
                        last_err = r.stderr[-200:] if r and r.stderr else 'mp4 failed'
                except Exception as ex:
                    last_err = str(ex)

            if not success:
                return {'success':False,'output_bytes':None,'filename':None,
                        'message':f'Soft-sub failed: {last_err[:200]}'}

        with open(out_path,'rb') as f:
            out_bytes = f.read()

        size_mb = len(out_bytes)/1024/1024
        log(f"✅ Done! {size_mb:.1f} MB")

        return {
            'success':      True,
            'output_bytes': out_bytes,
            'filename':     out_name,
            'message':      f'Done! ({size_mb:.1f} MB)'
        }

    except Exception as ex:
        return {'success':False,'output_bytes':None,
                'filename':None,'message':str(ex)}
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


# ===========================================================
# SESSION STATE
# ===========================================================

def init_state():
    for k,v in {
        'merge_type':   'hard',
        'num_eps':      1,
        'results':      [],
        'result_bytes': {},
        'seed':         0,
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()


# ===========================================================
# UI
# ===========================================================

# Header
st.markdown("""
<div style="text-align:center;padding:20px 0 10px">
  <div style="font-size:52px">🎬</div>
  <h1 style="font-size:28px;font-weight:800;margin:8px 0 4px;color:#fff">
    Video &amp; Subtitle Merger
  </h1>
  <p style="color:rgba(255,255,255,.5);font-size:14px">
    Upload MP4 + SRT for each episode — merge &amp; download instantly
  </p>
</div>
""", unsafe_allow_html=True)

# FFmpeg check
if not check_ffmpeg():
    st.error("""
**⚠️ FFmpeg not found!**

Add `ffmpeg` to `packages.txt` in your repository root:
