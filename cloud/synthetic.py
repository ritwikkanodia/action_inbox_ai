"""Offline test outcomes, not an agent or a real mail classifier."""


class SyntheticExecutor:
    def run(self, claim, context):
        return f'synthetic:{claim.job_id}'


class SyntheticGenerator:
    def generate(self, event):
        return 'skipped', None
