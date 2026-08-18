#!/usr/bin/env python3
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from moderation_bot.config import app_id, app_secret

BOT_APP_ID = app_id()
ROOT = Path(__file__).resolve().parents[1]
LAST_CARD = ROOT / 'moderation_bot/last_card.json'
LAST_PATCH = ROOT / 'moderation_bot/last_patch_card.json'
STATE_FILE = ROOT / 'moderation_bot/reaction_state.json'
CURRENT_CARD_STATE = ROOT / 'moderation_bot/current_card_state.json'
CARD_REGISTRY = ROOT / 'moderation_bot/card_registry.json'
STATUS_META = {
    'approved': {'label': '✅ Approved', 'emoji': 'DONE', 'button_type': 'primary', 'template': 'green'},
    'rejected': {'label': '❌ Rejected', 'emoji': 'CRY', 'button_type': 'primary', 'template': 'red'},
    'working': {'label': '🔄 Working on it', 'emoji': 'THINKING', 'button_type': 'primary', 'template': 'blue'},
    'dismiss': {'label': '🚫 Dismissed', 'emoji': None, 'button_type': 'default', 'template': 'grey'},
}


def get_token():
    data = json.dumps({'app_id': BOT_APP_ID, 'app_secret': app_secret()}).encode()
    req = urllib.request.Request('https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal', data=data, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.loads(r.read().decode())
    if res.get('code') != 0:
        raise RuntimeError(res)
    return res['tenant_access_token']


def api(url, method='GET', payload=None, token=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers['Content-Type'] = 'application/json; charset=utf-8'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode()
            print(method, url, txt[:2000], flush=True)
            return json.loads(txt)
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors='replace')
        print(method, url, 'HTTP_ERROR', e.code, txt[:4000], flush=True)
        raise RuntimeError(f'HTTP {e.code} body={txt}')


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def load_state():
    return load_json(STATE_FILE, {})


def save_state(state):
    save_json(STATE_FILE, state)


def load_card_for(card_message_id):
    all_cards = load_json(CURRENT_CARD_STATE, {})
    return all_cards.get(card_message_id) or load_json(LAST_CARD, {})


def save_card_for(card_message_id, card):
    all_cards = load_json(CURRENT_CARD_STATE, {})
    if card_message_id:
        all_cards[card_message_id] = card
    save_json(CURRENT_CARD_STATE, all_cards)


def latest_card_for(target):
    return load_json(CARD_REGISTRY, {}).get(target)


def add_reaction(original_message_id, emoji_type):
    return api(f'https://open.larksuite.com/open-apis/im/v1/messages/{original_message_id}/reactions', 'POST', {'reaction_type': {'emoji_type': emoji_type}}, get_token())


def delete_reaction(original_message_id, reaction_id):
    return api(f'https://open.larksuite.com/open-apis/im/v1/messages/{original_message_id}/reactions/{quote(reaction_id, safe="")}', 'DELETE', None, get_token())


def list_reactions(original_message_id):
    return api(f'https://open.larksuite.com/open-apis/im/v1/messages/{original_message_id}/reactions?page_size=50', 'GET', None, get_token())


def cleanup_bot_reactions(original_message_id):
    actions = []
    res = list_reactions(original_message_id)
    for item in (res.get('data') or {}).get('items', []) or []:
        op = item.get('operator') or {}
        if op.get('operator_id') == BOT_APP_ID or op.get('operator_type') == 'app':
            try:
                actions.append({'cleanup_delete': delete_reaction(original_message_id, item.get('reaction_id'))})
            except Exception as e:
                actions.append({'cleanup_delete_error': repr(e), 'reaction_id': item.get('reaction_id')})
    return actions


def update_card_state(card, selected_status, original_message_id, operator_name=None):
    card.setdefault('config', {})['wide_screen_mode'] = True
    card['config']['update_multi'] = True
    if selected_status:
        meta = STATUS_META[selected_status]
        template = meta['template']
        audit = f"**Selected status:** {meta['label']}"
    else:
        template = 'blue'
        audit = '**Selected status:** Not selected'
    if operator_name:
        audit += f" by {operator_name}"
    audit += f" at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    if 'header' in card:
        card['header']['template'] = template
    found = False
    for el in card.get('elements', []):
        if el.get('tag') == 'div' and (el.get('text') or {}).get('content', '').startswith('**Selected status:**'):
            el['text']['content'] = audit
            found = True
        if el.get('tag') == 'action':
            acts = el.setdefault('actions', [])
            if not any((b.get('value') or {}).get('action') == 'dismiss' for b in acts):
                acts.append({'tag': 'button', 'text': {'tag': 'plain_text', 'content': '🚫 Dismiss'}, 'type': 'default', 'value': {'action': 'dismiss', 'target_message_id': original_message_id}})
            for btn in acts:
                val = btn.setdefault('value', {})
                val['target_message_id'] = original_message_id
                btn['type'] = 'primary' if selected_status and val.get('action') == selected_status else 'default'
    if not found:
        card.setdefault('elements', []).insert(1, {'tag': 'div', 'text': {'tag': 'lark_md', 'content': audit}})
    return card


def patch_card(card_message_id, card):
    LAST_PATCH.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding='utf-8')
    save_card_for(card_message_id, card)
    attempt = 0
    while True:
        try:
            return api(f'https://open.larksuite.com/open-apis/im/v1/messages/{card_message_id}', 'PATCH', {'msg_type': 'interactive', 'content': json.dumps(card, ensure_ascii=False)}, get_token())
        except Exception as e:
            if attempt >= 2:
                print(f'patch_card: giving up after {attempt + 1} attempts, final error:', repr(e), flush=True)
                raise
            attempt += 1
            print(f'patch_card: attempt {attempt} failed ({e!r}); retrying in 2s', flush=True)
            time.sleep(2)


def process_action(value, card_message_id=None, operator_name=None):
    status = value.get('action') or value.get('status')
    original = value.get('target_message_id') or value.get('original_message_id')
    print('process_action args:', json.dumps({'card_message_id': card_message_id, 'status': status, 'original': original, 'value': value}, ensure_ascii=False), flush=True)
    if status not in STATUS_META or not original:
        raise ValueError(f'Bad callback value: {value}')
    latest = latest_card_for(original)
    if latest and card_message_id and latest != card_message_id:
        print('stale card ignored:', json.dumps({'clicked': card_message_id, 'latest': latest, 'target': original}, ensure_ascii=False), flush=True)
        return {'stale': True, 'toast': 'A newer card exists for this request — please use the latest one'}
    key = card_message_id or original
    state = load_state()
    prev = state.get(key)
    actions = []
    print('process_action prev_state:', json.dumps({'key': key, 'prev': prev}, ensure_ascii=False), flush=True)
    same = bool(prev and prev.get('status') == status)
    if status == 'dismiss':
        if same:
            selected = None
            state.pop(key, None)
            actions.append({'dismiss': 'toggled off (un-dismissed) — no reaction'})
        else:
            state[key] = {'status': 'dismiss', 'original_message_id': original}
            selected = 'dismiss'
            actions.append({'dismiss': 'marked dismissed — no reaction'})
        save_state(state)
        card = load_card_for(card_message_id)
        patch = patch_card(card_message_id, update_card_state(card, selected, original, operator_name)) if card_message_id and card else {'skipped': 'missing card'}
        return {'key': key, 'same_clicked': same, 'actions': actions, 'state': state.get(key), 'patch': patch}
    actions.extend(cleanup_bot_reactions(original))
    if same:
        selected = None
        state.pop(key, None)
    else:
        try:
            res = add_reaction(original, STATUS_META[status]['emoji'])
            rid = (res.get('data') or {}).get('reaction_id')
            state[key] = {'status': status, 'original_message_id': original, 'reaction_id': rid, 'emoji': STATUS_META[status]['emoji']}
            actions.append({'add_new': res})
        except Exception as e:
            actions.append({'add_new_error': repr(e)})
            print('add_new_error exact:', repr(e), flush=True)
            state.pop(key, None)
        selected = status
    save_state(state)
    card = load_card_for(card_message_id)
    patch = patch_card(card_message_id, update_card_state(card, selected, original, operator_name)) if card_message_id and card else {'skipped': 'missing card'}
    return {'key': key, 'same_clicked': same, 'actions': actions, 'state': state.get(key), 'patch': patch}
