"""Explicit paid smoke test through the real HTTP app; never reads credentials."""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        raise SystemExit('No calls made. Pass --live to allow real embedding/model calls.')
    root = Path(__file__).resolve().parents[2]
    pdf = root/'output/pdf/雾港来信-检索测试.pdf'
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = root/'artifacts'/f'live-smoke-{stamp}'
    out.mkdir(parents=True)
    results = {'started_at': stamp, 'steps': [], 'session_id': None, 'document_id': None}

    def save():
        (out/'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')

    def record(name, **details):
        results['steps'].append({'step': name, **details})
        save()
        print(json.dumps(results['steps'][-1], ensure_ascii=False), flush=True)

    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=600, trust_env=False) as client:
        health = client.get('/api/health').raise_for_status().json()
        if health.get('mode') != 'live-llm-live-retrieval':
            raise SystemExit('Real model and embedding configuration are required.')
        existing = client.get('/api/documents').raise_for_status().json()
        if any(d['title'] != pdf.name and not d['source_path'].startswith('demo://') for d in existing):
            raise SystemExit('Other documents present: stop to avoid sending private corpus in smoke test.')
        record('health', response=health)
        response = client.post('/api/documents/upload', params={'filename':pdf.name},
                               content=pdf.read_bytes(), headers={'Content-Type':'application/pdf'})
        response.raise_for_status()
        ident = response.json()['id']
        results['document_id'] = ident
        deadline = time.monotonic()+600
        while time.monotonic() < deadline:
            doc = client.get(f'/api/documents/{ident}').raise_for_status().json()
            if doc['status'] not in {'queued','parsing','embedding'}:
                break
            time.sleep(1)
        record('upload', response=doc)
        if doc['status'] != 'ready':
            raise SystemExit('Upload did not finish successfully; no questions sent.')
        session = client.post('/api/sessions', json={'title':f'真实模型联调 {stamp}'}).raise_for_status().json()
        results['session_id'] = sid = session['id']
        save()
        questions = [
            '如何凭闪光间隔和颜色辨别北岬灯塔与南岸导航灯？',
            '其中北岬灯塔两次闪光的间隔是多少秒？',
            '林遥大学就读的学校名称和毕业年份是什么？资料没有写就明确说无法确定，不要猜测。',
        ]
        for question in questions:
            print(json.dumps({'sending': question}, ensure_ascii=False), flush=True)
            start = time.monotonic()
            try:
                response = client.post(f'/api/sessions/{sid}/turns', json={'question':question,'top_k':3})
                data = response.json()
            except (httpx.HTTPError, ValueError):
                record('question', question=question, error='Request failed or timed out; not retried.', seconds=round(time.monotonic()-start,2))
                raise SystemExit('Read saved session before retrying any uncertain question.')
            record('question', question=question, status=response.status_code,
                   seconds=round(time.monotonic()-start,2), response=data)
            if not response.is_success or '格式校验' in data.get('answer',{}).get('text',''):
                raise SystemExit('Model/API integration failure; stopped further paid calls.')
        restored = client.get(f'/api/sessions/{sid}').raise_for_status().json()
        record('session_restore', turns=len(restored['turns']))
        for source in restored['turns'][0]['answer']['sources']:
            for page in source.get('pages') or [{'page_number':source['page_number']}]:
                number = page['page_number']
                image = client.get(f'/api/documents/{ident}/pages/{number}/image').raise_for_status()
                record('original_page', page=number, png=image.content.startswith(b'\x89PNG'))
        results['finished'] = True
        save()
        print(f'RESULTS: {out / "results.json"}', flush=True)


if __name__ == '__main__':
    main()
