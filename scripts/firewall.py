"""Manage only fake-print's tagged UFW entries. Run through the just recipes."""
import re
import subprocess
import sys

TAG = 'fake-print-managed-v1'


def ufw(*args):
    return subprocess.check_output(['sudo', 'env', 'LC_ALL=C', 'ufw', *args], text=True)


def entries():
    status = ufw('status', 'numbered')
    if not status.startswith('Status: active'):
        raise SystemExit('UFW is inactive; no rules changed.')
    return re.findall(r'^\[\s*(\d+)\]\s+(.+)$', status, re.M)


def manage(action, rules):
    listing = entries()
    if action == 'down':
        # Deleting higher indices first preserves the remaining rule numbers.
        for number, line in reversed(listing):
            if line.partition('#')[2].strip() == TAG:
                print(ufw('--force', 'delete', number), end='')
        return
    for rule in dict.fromkeys(rules):
        if rule.startswith('0/'):
            continue
        if any(line.split()[0] in (rule, rule.split('/')[0]) for _, line in listing):
            print(f'Leaving existing {rule} rule unchanged')
            continue
        print(ufw('allow', rule, 'comment', TAG), end='')


if __name__ == '__main__':
    manage(sys.argv[1], sys.argv[2:])
