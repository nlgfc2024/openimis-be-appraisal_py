from django.core.management.base import BaseCommand, CommandError

from appraisal import services
from appraisal.apps import AppraisalConfig


class Command(BaseCommand):
    help = ("Create or refresh the appraisals of applications already imported: those from before the module "
            "was installed or a programme configured, or missed by a failed signal. Each person's latest imported "
            "application is used. Safe to run again: officers' answers are never changed.")

    def add_arguments(self, parser):
        parser.add_argument("--programme", help="Only this programme (default: every configured programme)")
        parser.add_argument("--dry-run", action="store_true", help="Count the applications, change nothing")

    def handle(self, *args, programme=None, dry_run=False, **options):
        from msr_etl.models import KoboSubmission

        programmes = AppraisalConfig.programmes or {}
        if programme and programme not in programmes:
            configured = ", ".join(programmes) or "none"
            raise CommandError(f"Programme {programme} is not configured; configured: {configured}")
        sources = {source for code, config in programmes.items() if not programme or code == programme
                   for source in config.get("source_types", [])}
        latest = (KoboSubmission.objects
                  .filter(source_type__in=sources, individual__isnull=False,
                          status__in=[KoboSubmission.Status.IMPORTED, KoboSubmission.Status.UPDATED])
                  .order_by("individual_id", "-kobo_id").distinct("individual_id"))
        count = latest.count()
        if dry_run:
            self.stdout.write(f"{count} applications would be appraised")
            return
        appraisals = 0
        for submission in latest.iterator(chunk_size=200):
            appraisals += len(services.apply_submission(submission, programme=programme))
        self.stdout.write(self.style.SUCCESS(f"{count} applications, {appraisals} appraisals created or refreshed"))
