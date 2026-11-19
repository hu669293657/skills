"""Run only the frozen actions bundled by the customer planner, never re-plan."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

from .linux import LinuxHost, cpulist
from .network import parse_interrupts
from .planner import BindError, digest
from .transaction import check_context, load_json, same_identity, save_json


def check(condition, message):
    if not condition:
        raise BindError(message)


class Targets:
    def __init__(self, plan, host=None):
        self.plan = plan
        self.host = host or LinuxHost()

    def context(self):
        check_context(self.plan['context'], self.host.context())

    def inventory(self):
        self.context()
        devices, owners = self.host.npu({})
        check(self.host.topology(devices)==self.plan['topology'], 'CPU/SMT/NPU topology drift')
        check({str(key):value for key,value in owners.items()}==self.plan['ownership'], 'NPU ownership drift')
        for pid,tids in self.plan['members'].items():
            directory=self.host.proc/pid/'task'
            check(sorted(int(task.name) for task in directory.iterdir())==tids,'Thread membership drift: '+pid)
        for child in self.plan['children']:
            current=child['pid']; seen=set()
            while current>1 and current not in seen and current!=child['owner_pid']:
                seen.add(current); current=self.host.stat(current)['ppid']
            check(current==child['owner_pid'],'Child ownership drift')
        if self.plan['irq_actions']:
            status=subprocess.run(['systemctl','show','irqbalance.service','-p','ActiveState','--value'],
                                  capture_output=True,text=True,timeout=5,check=False)
            check(status.returncode==0 and status.stdout.strip()=='inactive','irqbalance active/unknown; do not stop automatically')

    def irq_dir(self, action):
        irq=str(action['irq']); device=(self.host.sys/'class/net'/action['nic']/'device' if action['nic'] else self.host.sys/'bus/pci/devices'/action['pci_bdf'])
        check(str(device.resolve(strict=True))==action['pci'] and (device/'msi_irqs'/irq).exists(), 'PCI/MSI IRQ identity drift: '+irq)
        check(int((device/'numa_node').read_text())==action['node'] and
              set(action['target'])<=set(cpulist((device/'local_cpulist').read_text())), 'IRQ locality drift: '+irq)
        _,rows=parse_interrupts((self.host.proc/'interrupts').read_text()); row=rows.get(irq)
        check(row and row['name']==action['name'],'Interrupt label changed: '+irq)
        return self.host.proc/'irq'/irq

    def masks(self,action):
        if action['kind']=='irq':
            path=self.irq_dir(action)
            return [cpulist((path/key).read_text()) for key in ('smp_affinity_list','effective_affinity_list')]
        pid,tid=action['pid'],action['tid']; ident=self.host.identity(pid,tid)
        check(same_identity(action,ident) and ident['ceiling']==action['ceiling'], 'Task identity or cpuset drift: %s'%tid)
        check(self.host.read(self.host.proc/str(pid)/'task'/str(tid)/'cgroup')==action['cgroup'], 'Task cgroup changed: %s'%tid)
        return [sorted(self.host.get_affinity(tid))]

    def write(self,action,cpus):
        if action['kind']=='irq':
            (self.irq_dir(action)/'smp_affinity_list').write_text(','.join(map(str,cpus))+'\n')
        else:
            self.host.set_affinity(action['tid'],cpus)


def original_masks(action):
    return [action['original'],action['effective']] if action['kind']=='irq' else [action['original']]


def preflight(plan,targets,applied=False):
    targets.inventory()
    for action in plan['actions']+plan['irq_actions']:
        masks=targets.masks(action)
        expected=[action['target']]*len(masks) if applied else original_masks(action)
        check(masks==expected,'Affinity drift at '+str(action.get('tid',action.get('irq'))))


def rollback(plan,targets,path,journal):
    targets.context()
    check(journal.get('plan_id')==digest(plan), 'Journal belongs to a different plan')
    actions=plan['actions']+plan['irq_actions']; errors=[]
    pending=journal['pending']
    check(isinstance(pending,list) and len(set(pending))==len(pending) and
          all(type(idx) is int and 0<=idx<len(actions) for idx in pending),'Invalid journal')
    for idx in reversed(pending[:]):
        action=actions[idx]
        try:
            masks=targets.masks(action)
            if masks!=original_masks(action):
                check(masks[0]==action['target'],'Third-party affinity; left untouched')
                targets.write(action,action['original'])
                check(targets.masks(action)==original_masks(action),'Original affinity not fully restored')
            pending.remove(idx)
        except (ValueError,OSError,RuntimeError) as exc:
            errors.append('%s: %s'%(idx,exc))
        save_json(path,journal)
    journal.update(status='INCOMPLETE' if errors else 'RESTORED',errors=errors); save_json(path,journal)
    check(not errors,'Rollback incomplete: '+'; '.join(errors))


def apply(plan,targets,path):
    preflight(plan,targets)
    journal=dict(plan_id=digest(plan),pending=[],status='APPLYING', originals=[original_masks(action) for action in plan['actions']+plan['irq_actions']])
    save_json(path,journal,exclusive=True)
    try:
        for idx,action in enumerate(plan['actions']+plan['irq_actions']):
            masks=targets.masks(action)
            check(masks==original_masks(action),'Concurrent affinity change')
            if all(mask==action['target'] for mask in masks): continue
            journal['pending'].append(idx); save_json(path,journal)
            targets.write(action,action['target'])
            check(all(mask==action['target'] for mask in targets.masks(action)), 'Requested/effective mask mismatch')
        preflight(plan,targets,applied=True)
        journal['status']='APPLIED'; save_json(path,journal)
    except (Exception,KeyboardInterrupt):
        rollback(plan,targets,path,journal)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',nargs='?',default='preview',choices=('show','preview','apply','verify','rollback'))
    parser.add_argument('--expect-plan'); parser.add_argument('--state',type=Path)
    parser.add_argument('--include-customers',action='store_true'); parser.add_argument('--include-irqs',action='store_true')
    args=parser.parse_args()
    try:
        with ZipFile(sys.argv[0]) as archive: plan=json.loads(archive.read('customer_plan.json'))
        check(not args.expect_plan or digest(plan)==args.expect_plan,'Wrong plan bundle')
        if args.mode=='show':
            print(json.dumps(plan,ensure_ascii=True,indent=2)); return 0
        check(sys.platform=='linux','Live commands require the customer Linux host; use show for offline preview')
        targets=Targets(plan)
        if args.mode in ('apply','rollback'):
            check(args.state is not None,'Specify --state PATH in a trusted private directory')
            if args.mode=='apply':
                check(args.include_customers or not any(row['kind']=='customer' for row in plan['actions']), 'Review customer moves and pass --include-customers')
                check(args.include_irqs or not plan['irq_actions'],'Review IRQ moves and pass --include-irqs')
                apply(plan,targets,args.state)
            else: rollback(plan,targets,args.state,load_json(args.state))
        else: preflight(plan,targets,applied=args.mode=='verify')
        print('%s OK: %d TIDs, %d IRQs; %s'%(args.mode,len(plan['actions']),len(plan['irq_actions']),digest(plan)))
        return 0
    except (ValueError,OSError,RuntimeError,KeyError,subprocess.SubprocessError) as exc:
        print('BLOCKED: '+str(exc),file=sys.stderr);return 2
