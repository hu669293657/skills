"""Render compatibility cards and recommendations from reviewed version evidence."""
from html import escape
from pathlib import Path

from check_compatibility import SCENES, best_plans, read_object, remediation, version


LABELS = {
    'vllm_ascend': 'vLLM Ascend', 'cann': 'CANN', 'torch_npu': 'TorchNPU 安装包',
    'torch': 'PyTorch', 'vllm': 'vLLM', 'mindspeed_llm': 'MindSpeed LLM',
    'mindspeed': 'MindSpeed', 'modellink': 'ModelLink', 'mindie': 'MindIE',
    'triton_ascend': 'Triton Ascend', 'megatron_core': 'Megatron Core',
    'fsdpturbo': 'FSDPTurbo', 'hdk': 'HDK', 'python': 'Python',
}


def expected_versions(values):
    return ' / '.join(
        '≥ ' + value.replace('..', ' 且 < ') if '..' in value else value
        for value in values
    )


def change_list(changes, show_target=True):
    items = []
    for key, check in changes.items():
        target = f' → <code>{escape(expected_versions(check["expected"]))}</code>' if show_target else ''
        items.append(
            f'<li><strong>{escape(LABELS[key])}</strong> <code>{escape(check["observed"])}</code>{target}</li>'
        )
    return '<ul class="compatibility-changes">' + ''.join(items) + '</ul>'


def alternatives(plans):
    first, *others = plans
    if not others:
        return first
    return (first + f'<details><summary>查看其余 {len(others)} 个并列组合</summary>'
            + ''.join(others) + '</details>')


def recommend_markup(versions, matrix, scene):
    current, recommended = remediation(versions, matrix, scene)
    if not recommended:
        return ''
    anchor = LABELS[SCENES[scene]]
    current_release = escape(versions[SCENES[scene]])
    current_options = [change_list(plan['changes'], show_target=False) for plan in current]
    if len(current_options) > 1:
        current_options = [
            f'<p>{escape(plan["rule"])}</p>' + changes
            for plan, changes in zip(current, current_options)
        ]
    options = [
        f'<div class="compatibility-result"><h4>{escape(anchor)} {escape(plan["release"])}</h4>'
        + change_list(plan['changes'])
        + ('<p>需另行核对 ' + escape('、'.join(LABELS[key] for key in plan['uncovered']))
           + ' 的适配范围。</p>' if plan['uncovered'] else '')
        + '</div>'
        for plan in recommended
    ]
    count = len(recommended[0]['changes'])
    return (
        '<div class="banner warn"><div>'
        f'<p><strong>当前 {escape(anchor)} {current_release} 下偏离基线的版本</strong></p>'
        + alternatives(current_options)
        + '</div></div>'
        + f'<div class="sub-head">推荐改动 {count} 项</div>'
        + '<p class="compatibility-note">按已采集且可核对的组件版本计算。</p>'
        + alternatives(options)
    )


def plan_markup(plan, keep):
    rows = []
    for key, check in plan['checks'].items():
        if key == keep:
            continue
        label = LABELS[key] + ('（可选）' if key in plan['optional'] else '')
        current = escape(check['observed'] or '')
        target = escape(expected_versions(check['expected']))
        if check['status'] == 'matched':
            target = f'{current} <span class="val-add">保留</span>'
        elif check['status'] == 'deviation':
            target = f'<span class="val-mod">{target}</span>'
        rows.append(f'<tr><th scope="row">{escape(label)}</th><td>{current}</td><td>{target}</td></tr>')
    notes = []
    if 'product' in plan:
        notes.append(f'<p>TorchNPU 产品版本 {escape(plan["product"])}</p>')
    for key, value in plan.get('package_baseline', {}).items():
        notes.append(f'<p>{escape(LABELS[key])} 发布包基线 {escape(value)}</p>')
    if plan['uncovered']:
        names = '、'.join(LABELS[key] for key in plan['uncovered'])
        notes.append(f'<p>此组合需另行核对 {escape(names)} 的适配范围。</p>')
    return (
        '<div class="compatibility-result compatibility-plan">'
        '<table class="compatibility-table"><thead><tr>'
        '<th scope="col">配套组件</th><th scope="col">当前版本</th><th scope="col">推荐版本</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        + ''.join(notes) + '</div>'
    )


def component_cards(index, scene, versions, matrix):
    buttons, dialogs = [], []
    for key, actual in versions.items():
        label = LABELS[key]
        template_id = f'compatibility-card-{index}-{key}'
        title = f'{label} {actual}'
        buttons.append(
            f'<button type="button" class="comp-card compatibility-card" aria-haspopup="dialog" '
            f'data-compatibility-template="{template_id}" data-compatibility-title="{escape(title, quote=True)}">'
            f'<span class="comp-name">{escape(label)}</span>'
            f'<span class="comp-version">{escape(actual)}</span></button>'
        )
        plans = best_plans(versions, matrix, scene, keep=key)
        if plans:
            count = len(plans[0]['changes'])
            body = (
                f'<p class="compatibility-note">保留当前版本。已采集版本中建议改动 {count} 项。</p>'
                + alternatives([plan_markup(plan, key) for plan in plans])
            )
        else:
            body = '<p class="compatibility-note">离线规则未覆盖该场景下的此版本。</p>'
        dialogs.append(f'<template id="{template_id}">{body}</template>')
    return '<div class="comp-grid">' + ''.join(buttons) + '</div>' + ''.join(dialogs)


def render_environments(environments, mode):
    if not isinstance(environments, list):
        raise ValueError('COMPATIBILITY_ENVIRONMENTS: expected an array')
    if not environments:
        return ''
    path = Path(__file__)
    matrix = read_object(path.with_name('compatibility_matrix.json'))
    sections = []
    for index, item in enumerate(environments):
        if not isinstance(item, dict):
            raise ValueError('COMPATIBILITY_ENVIRONMENTS: expected environment objects')
        name, scene, evidence = (item.get(key) for key in ('name', 'scene', 'versions'))
        if not (isinstance(name, str) and name.strip() and isinstance(scene, str)
                and scene in SCENES and isinstance(evidence, dict)):
            raise ValueError('COMPATIBILITY_ENVIRONMENTS: expected name, scene and versions')
        for key, actual in evidence.items():
            if key not in LABELS or not version(actual, key):
                raise ValueError(f'COMPATIBILITY_ENVIRONMENTS: invalid observed version for {key}')
        versions = {key: version(evidence[key], key) for key in LABELS if key in evidence}
        if versions:
            heading = (f'<h3 class="sub-head">{escape(name)}</h3>'
                       if mode == 'comparison' or len(environments) > 1 else '')
            sections.append(
                '<div class="compatibility-environment">' + heading
                + component_cards(index, scene, versions, matrix)
                + recommend_markup(versions, matrix, scene) + '</div>'
            )
    if not sections:
        return ''
    partial = path.with_name('compatibility_lookup.html').read_text(encoding='utf-8')
    return partial.replace('<!-- COMPATIBILITY_ENVIRONMENTS -->', ''.join(sections))
