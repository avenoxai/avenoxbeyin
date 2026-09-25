"""Source-grounded, optional typed memory triage. Never writes canonical memory."""
from datetime import datetime, timezone
import json

import beyin_v3_jev_client as client
from beyin_v3 import rejected_matches
from beyin_v3_jev import (_eligible, _verified_evidence, _safe, _signature, _calibration,
                          context_limit, limits, remote_allowed, source_context)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def assess_memory(store, proposal, *, project, transport=None):
    """Assess support, commitment, kind and prior relationships in ONE typed request.

    The route only advises the active agent to inspect sources or consider a
    candidate. Even a unanimous high-confidence result cannot approve, save,
    supersede a record, create a task, or mark a task completed.

    A claim that restates a rejected inference or preference is never a candidate,
    whatever the advisor said and whether or not it ran. Only the rejected record
    ids are reported; the rejected text never enters the advisor request.
    """
    result = _assess(store, proposal, project=project, transport=transport)
    rejected = rejected_matches(store, proposal['claim'], project)
    if rejected:
        result['previously_rejected'] = [match['record_id'] for match in rejected]
        # A new list: the degraded paths share theirs with the advisor payload.
        result['diagnostics'] = result['diagnostics'] + ['previously_rejected']
        if result['route'] == 'candidate_for_agent_review':
            result['route'] = 'inspect_sources'
    return result


def _assess(store, proposal, *, project, transport):
    required = {'status', 'project', 'claim', 'evidence'}
    if not isinstance(proposal, dict) or not required <= set(proposal) or set(proposal) - required - {'prior_record_ids'}:
        raise ValueError('invalid_proposal_fields')
    raw = json.dumps(proposal, ensure_ascii=False)
    if len(raw) > 24000:
        raise ValueError('proposal_too_large')
    proposal = json.loads(raw)
    if (not isinstance(project, str) or not project.strip() or proposal['project'] != project or
            proposal['status'] != 'proposed' or not isinstance(proposal['claim'], str) or not proposal['claim'].strip()):
        raise ValueError('proposal_scope_or_status_invalid')
    evidence = proposal['evidence']
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
        raise ValueError('proposal_requires_evidence')
    eligible = _eligible(store, project)
    refs = _verified_evidence(store, evidence, project, eligible)
    prior_ids = proposal.get('prior_record_ids')
    if prior_ids is None:
        evidence_ids = {r['id'] for r in refs}
        prior_ids = [r['id'] for r in store.candidates(proposal['claim'], project=project, strict=True, limit=8)
                     if r['id'] not in evidence_ids][:4]
    if (not isinstance(prior_ids, list) or len(prior_ids) > 4 or
            any(not isinstance(ident, str) or ident not in eligible for ident in prior_ids) or
            len(set(prior_ids)) != len(prior_ids)):
        raise ValueError('prior_not_current_or_in_scope')
    priors = [eligible[ident] for ident in prior_ids]
    result = dict(status='advisory_only', mechanical_verified=True, approved=False, memory_written=False,
                  task_created=False, task_completed=False, route='local_source_review',
                  dimensions={}, prior_relations=[], diagnostics=[])
    mode = client.inspect_config(store.state_dir)
    if not mode['valid'] or mode['mode'] == 'off' or 'review' not in mode.get('features', []):
        result['diagnostics'] = ['semantic_off' if mode['valid'] else 'config_invalid']
        return result
    all_records = refs + priors
    if not all(remote_allowed(r) for r in all_records):
        result['diagnostics'] = ['source_local_only']
        return result
    # A long source is NOT silently summarized/truncated into seemingly complete evidence.
    if any(len(source_context(r)) > context_limit(mode) for r in all_records):
        result['diagnostics'] = ['source_context_incomplete']
        return result
    item = dict(claim=proposal['claim'], project=project, evidence=[
        dict(quote=e['quote'], context=source_context(r), updated_at=r.get('updated_at', ''))
        for e, r in zip(evidence, refs)], prior=[
        dict(statement=source_context(r), updated_at=r.get('updated_at', '')) for r in priors])
    try:
        _safe(store, item)
    except (ValueError, OSError):
        result['diagnostics'] = ['advisor_sensitive_or_invalid_input']
        return result
    before = _signature(all_records)
    advice = client.evaluate(store.state_dir, '', [item], purpose='memory_assessment',
                             scope='project:' + project, source_versions=before, transport=transport)
    result['jev'] = advice
    current = _eligible(store, project)
    after = [current[r['id']] for r in all_records if r['id'] in current]
    if before != _signature(after) or client.inspect_config(store.state_dir) != mode:
        result.update(mechanical_verified=False, diagnostics=['source_or_configuration_changed'])
        # Do not expose stale verdicts as usable decisions.
        result['jev'] = dict(mode=mode['mode'], degraded=True, diagnostics=result['diagnostics'])
        return result
    if advice['degraded']:
        result['diagnostics'] = advice['diagnostics']
        return result
    if advice['mode'] != 'on':
        result['diagnostics'] = ['shadow_not_applied']
        return _calibration(result, mode)
    choices = advice['relations']
    result['dimensions'] = {name: choices[name] for name in ('support', 'commitment', 'kind')}
    result['prior_relations'] = [dict(record_id=record['id'], **choices['relation_p' + str(i)])
                                 for i, record in enumerate(priors)]
    _calibration(result, mode)
    uncertain = any(item['confidence'] < limits(mode)['confidence'] for item in choices.values())
    conflict = any(relation['choice'] == 'contradiction' for relation in result['prior_relations'])
    if conflict:
        result['diagnostics'].append('prior_conflict')
    for record, relation in zip(priors, result['prior_relations']):
        if relation['choice'] == 'changed_decision':
            old = _time(record.get('updated_at'))
            dates = [_time(r.get('updated_at')) for r in refs]
            if old is None or any(date is None or date < old for date in dates):
                uncertain = True
                result['diagnostics'].append('temporal_order_unverified')
    if (uncertain or conflict or choices['support']['choice'] != 'supports' or
            choices['commitment']['choice'] != 'asserted' or choices['kind']['choice'] in ('question', 'hypothesis', 'other')):
        result['route'] = 'inspect_sources'
        if uncertain:
            result['diagnostics'].append('uncertain_decision')
    else:
        result['route'] = 'candidate_for_agent_review'
    return result
