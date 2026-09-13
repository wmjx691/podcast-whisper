"""Explicit production composition of the accepted pipeline; import is inert.

No bootstrap or processing policy lives here. Cloud/model factories are invoked
only by run(), after complete configuration validation. Tests inject those seams.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit


REQUIRED = tuple('PODCAST_' + name for name in (
    'FEED_NAMESPACE RSS_URL CUTOVER_AT DRIVE_TOKEN_JSON DRIVE_ARTIFACT_FOLDER_ID '
    'DRIVE_SNAPSHOT_FOLDER_ID GCP_PROJECT_ID GCS_AUTHORITY_BUCKET GCS_AUTHORITY_OBJECT '
    'AUTH_OWNER AUTH_LEASE_SECONDS AUTH_RENEW_INTERVAL_SECONDS AUTH_SAFETY_MARGIN_SECONDS '
    'AUTH_CLOCK_UNCERTAINTY_SECONDS AUTH_REQUEST_DEADLINE_SECONDS AUTH_RECONCILIATION_READS '
    'RSS_TIMEOUT_SECONDS AUDIO_TIMEOUT_SECONDS WORK_ROOT').split())
REPORT_FIELDS = ('processed', 'recovered', 'skipped', 'failed', 'pending', 'run_errors')


class RuntimeFailure(RuntimeError):
    """Public reason codes only; never include dependency exceptions or values."""


def _url(value, *, https_only=False):
    try:
        u = urlsplit(value)
        return (u.scheme in (('https',) if https_only else ('https', 'http'))
                and bool(u.hostname) and u.port != 0 and not u.username and not u.password
                and not u.fragment and not any(c.isspace() or ord(c) < 32 for c in value))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Config:
    values: dict = field(repr=False)
    drive_info: dict = field(repr=False)
    cutover_at: datetime
    timing: object
    clock_uncertainty: float
    rss_timeout: float
    audio_timeout: float

    def get(self, name):
        return self.values['PODCAST_' + name]

    @classmethod
    def from_env(cls, environ):
        values = {}
        for name in REQUIRED:
            value = environ.get(name)
            if not isinstance(value, str) or not value.strip() or '\x00' in value:
                raise RuntimeFailure('config_invalid:' + name)
            values[name] = value
        def get(name):
            return values['PODCAST_' + name]
        def invalid(name):
            raise RuntimeFailure('config_invalid:PODCAST_' + name) from None
        if get('FEED_NAMESPACE') != 'podcast-whisper-primary':
            invalid('FEED_NAMESPACE')
        try:
            cutover = datetime.fromisoformat(get('CUTOVER_AT').replace('Z', '+00:00'))
            if cutover.utcoffset() is None:
                invalid('CUTOVER_AT')
        except ValueError:
            invalid('CUTOVER_AT')
        if not _url(get('RSS_URL'), https_only=True):
            invalid('RSS_URL')
        if get('DRIVE_ARTIFACT_FOLDER_ID') == get('DRIVE_SNAPSHOT_FOLDER_ID'):
            invalid('DRIVE_SNAPSHOT_FOLDER_ID')
        work_root = Path(get('WORK_ROOT'))
        if not work_root.is_absolute():
            invalid('WORK_ROOT')
        # Read-only topology validation, before any production composition.
        for component in (work_root, *work_root.parents):
            try:
                mode = component.stat().st_mode
            except FileNotFoundError:
                continue
            except OSError:
                invalid('WORK_ROOT')
            if not stat.S_ISDIR(mode):
                invalid('WORK_ROOT')
        numbers = {}
        for name in ('AUTH_LEASE_SECONDS', 'AUTH_RENEW_INTERVAL_SECONDS',
                     'AUTH_SAFETY_MARGIN_SECONDS', 'AUTH_CLOCK_UNCERTAINTY_SECONDS',
                     'AUTH_REQUEST_DEADLINE_SECONDS', 'RSS_TIMEOUT_SECONDS', 'AUDIO_TIMEOUT_SECONDS'):
            try:
                value = float(get(name))
                if not math.isfinite(value) or value < 0 or (value == 0 and name != 'AUTH_CLOCK_UNCERTAINTY_SECONDS'):
                    invalid(name)
                numbers[name] = value
            except ValueError:
                invalid(name)
        try:
            reads = int(get('AUTH_RECONCILIATION_READS'))
        except ValueError:
            invalid('AUTH_RECONCILIATION_READS')
        # main establishes the accepted modules' existing absolute-import path.
        import main as application
        from authority_coordinator import TimingPolicy
        try:
            timing = TimingPolicy(numbers['AUTH_LEASE_SECONDS'], numbers['AUTH_RENEW_INTERVAL_SECONDS'],
                                  numbers['AUTH_SAFETY_MARGIN_SECONDS'], numbers['AUTH_REQUEST_DEADLINE_SECONDS'], reads)
        except ValueError:
            invalid('AUTH_TIMING_POLICY')
        uncertainty = numbers['AUTH_CLOCK_UNCERTAINTY_SECONDS']
        if uncertainty > timing.safety_margin:
            invalid('AUTH_CLOCK_UNCERTAINTY_SECONDS')
        try:
            def unique(pairs):
                result = {}
                for k, v in pairs:
                    if k in result:
                        raise ValueError('duplicate')
                    result[k] = v
                return result
            info = json.loads(get('DRIVE_TOKEN_JSON'), object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(info, dict) or info.get('type', 'authorized_user') != 'authorized_user':
                invalid('DRIVE_TOKEN_JSON')
            for key in ('refresh_token', 'client_id', 'client_secret'):
                if not isinstance(info.get(key), str) or not info[key].strip():
                    invalid('DRIVE_TOKEN_JSON')
            for key in ('token', 'token_uri', 'expiry'):
                if key in info and (not isinstance(info[key], str) or not info[key].strip()):
                    invalid('DRIVE_TOKEN_JSON')
            if 'expiry' in info:
                datetime.strptime(info['expiry'].rstrip('Z').split('.')[0], '%Y-%m-%dT%H:%M:%S')
            if 'scopes' in info:
                scopes = info['scopes']
                if not ((isinstance(scopes, str) and scopes.strip()) or
                        (isinstance(scopes, list) and scopes and
                         all(isinstance(s, str) and s.strip() for s in scopes))):
                    invalid('DRIVE_TOKEN_JSON')
            if 'token_uri' in info and not _url(info['token_uri'], https_only=True):
                invalid('DRIVE_TOKEN_JSON')
        except (ValueError, TypeError, RecursionError):
            invalid('DRIVE_TOKEN_JSON')
        return cls(values, info, cutover, timing, uncertainty,
                   numbers['RSS_TIMEOUT_SECONDS'], numbers['AUDIO_TIMEOUT_SECONDS'])


def _session():
    import requests
    session = requests.Session()
    session.trust_env = False
    session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
    session.mount('http://', requests.adapters.HTTPAdapter(max_retries=0))
    return session


class BoundedRequest:
    def __init__(self, deadline):
        from google.auth.transport.requests import Request
        self.request = Request(session=_session())
        self.deadline = deadline

    def __call__(self, *args, **kwargs):
        kwargs['timeout'] = self.deadline
        return self.request(*args, **kwargs)


class AuthTransport:
    """No automatic replay of an authenticated resource mutation."""
    def __init__(self, credentials, deadline):
        from google.auth.transport.requests import AuthorizedSession
        self.session = AuthorizedSession(credentials, auth_request=BoundedRequest(deadline),
                                         max_refresh_attempts=0, refresh_timeout=deadline)
        self.session.trust_env = False

    def request(self, method, url, *, timeout, **kwargs):
        return self.session.request(method, url, timeout=timeout,
                                    max_allowed_time=timeout, **kwargs)


class DriveHTTP:
    def __init__(self, transport, deadline):
        self.transport, self.deadline = transport, deadline

    def request(self, uri, method='GET', body=None, headers=None, **kwargs):
        import httplib2
        response = self.transport.request(method, uri, timeout=self.deadline,
                                          allow_redirects=False, data=body, headers=headers)
        metadata = dict(response.headers)
        metadata['status'] = str(response.status_code)
        return httplib2.Response(metadata), response.content


def build_drive(config, *, credentials_factory=None, request_factory=None,
                transport_factory=None, service_builder=None):
    try:
        if credentials_factory is None:
            from google.oauth2.credentials import Credentials
            credentials_factory = Credentials.from_authorized_user_info
        if service_builder is None:
            from googleapiclient.discovery import build
            service_builder = build
        credentials = credentials_factory(dict(config.drive_info))
        if not credentials.refresh_token:
            raise RuntimeFailure('drive_auth_unavailable')
        if not credentials.valid:
            credentials.refresh((request_factory or BoundedRequest)(config.timing.request_deadline))
        if not credentials.valid:
            raise RuntimeFailure('drive_auth_unavailable')
        transport = (transport_factory or AuthTransport)(credentials, config.timing.request_deadline)
        service = service_builder('drive', 'v3', http=DriveHTTP(transport, config.timing.request_deadline),
                                  cache_discovery=False, static_discovery=True)
        def execute(request, deadline):
            started = time.monotonic()
            result = request.execute(http=DriveHTTP(transport, deadline), num_retries=0)
            if time.monotonic() - started > deadline:
                raise RuntimeFailure('drive_request_deadline_exceeded')
            return result
        return service, execute
    except Exception:
        raise RuntimeFailure('drive_auth_unavailable') from None


def build_gcs(config, *, adc=None, transport_factory=None):
    try:
        if adc is None:
            import google.auth
            adc = google.auth.default
        credentials, _ = adc(scopes=['https://www.googleapis.com/auth/devstorage.read_write'],
                             request=BoundedRequest(config.timing.request_deadline))
        return (transport_factory or AuthTransport)(credentials, config.timing.request_deadline)
    except Exception:
        raise RuntimeFailure('gcs_auth_unavailable') from None


class Clock:
    def __init__(self, uncertainty):
        self.uncertainty = uncertainty

    def sample(self):
        from authority_coordinator import ClockReading
        return ClockReading(time.time(), time.monotonic(), self.uncertainty)


def fetch_rss(url, timeout):
    try:
        start = time.monotonic()
        with _session() as session:
            with session.get(url, timeout=timeout, allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise RuntimeFailure('rss_http_failure')
                chunks = []
                for chunk in response.iter_content(chunk_size=65536):
                    if time.monotonic() - start > timeout:
                        raise RuntimeFailure('rss_deadline_exceeded')
                    chunks.append(chunk)
                if time.monotonic() - start > timeout:
                    raise RuntimeFailure('rss_deadline_exceeded')
                return b''.join(chunks)
    except Exception:
        raise RuntimeFailure('rss_fetch_failed') from None


def fetch_audio(url, path, timeout):
    try:
        start = time.monotonic()
        with _session() as session:
            with session.get(url, timeout=timeout, allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise RuntimeFailure('audio_http_failure')
                with path.open('xb') as output:
                    for chunk in response.iter_content(chunk_size=65536):
                        if time.monotonic() - start > timeout:
                            raise RuntimeFailure('audio_deadline_exceeded')
                        output.write(chunk)
                    if time.monotonic() - start > timeout:
                        raise RuntimeFailure('audio_deadline_exceeded')
    except Exception:
        raise RuntimeFailure('audio_fetch_failed') from None


def build_transcriber(**kwargs):
    from transcriber import PodcastTranscriber
    return PodcastTranscriber(**kwargs)


@dataclass
class Dependencies:
    drive: object = build_drive
    gcs: object = build_gcs
    rss: object = fetch_rss
    audio: object = fetch_audio
    transcriber: object = build_transcriber
    clock: object = Clock


def compose(config, dependencies):
    import main as application
    from authority_coordinator import AuthorityCoordinator
    from drive_candidate_store import upload_candidate
    from drive_snapshot_store import DriveSnapshotStore
    from episode_identity import AttemptReference
    from gcs_authority_store import GCSAuthorityStore
    from rss_parser import parse_published_observations
    from transcript_candidate import build_candidate, checkpoint, current_execution

    service, execute = dependencies.drive(config)
    transport = dependencies.gcs(config)
    for folder in (config.get('DRIVE_ARTIFACT_FOLDER_ID'), config.get('DRIVE_SNAPSHOT_FOLDER_ID')):
        metadata = execute(service.files().get(fileId=folder,
            fields='id,mimeType,trashed,capabilities(canAddChildren)'), config.timing.request_deadline)
        if (not isinstance(metadata, dict) or metadata.get('id') != folder
                or metadata.get('mimeType') != 'application/vnd.google-apps.folder'
                or metadata.get('trashed') is not False
                or metadata.get('capabilities', {}).get('canAddChildren') is not True):
            raise RuntimeFailure('production_drive_folder_invalid')
    authority = GCSAuthorityStore(transport=transport, bucket=config.get('GCS_AUTHORITY_BUCKET'),
                                  object_name=config.get('GCS_AUTHORITY_OBJECT'), feed=config.get('FEED_NAMESPACE'))
    snapshots = DriveSnapshotStore(service=service, execute=execute,
        folder_id=config.get('DRIVE_SNAPSHOT_FOLDER_ID'), feed=config.get('FEED_NAMESPACE'))
    clock = dependencies.clock(config.clock_uncertainty)
    def coordinator():
        return AuthorityCoordinator(feed=config.get('FEED_NAMESPACE'), owner=config.get('AUTH_OWNER'),
            authority_store=authority, snapshot_store=snapshots, clock=clock, timing=config.timing)
    def discover():
        try:
            payload = dependencies.rss(config.get('RSS_URL'), config.rss_timeout)
            return parse_published_observations(payload, feed_namespace=config.get('FEED_NAMESPACE'))
        except Exception:
            raise RuntimeFailure('rss_discovery_failed') from None
    transcriber = None
    def work(requirement, reference):
        nonlocal transcriber
        # Durable observation retains its exact enclosure, including during recovery.
        # Local enrichment is work input only; returned candidate references retain
        # the original accepted association (no durable observation is rewritten).
        try:
            url = reference.observation.enclosure_url
            if not _url(url):
                with checkpoint('audio_fetch'):
                    raise RuntimeFailure('enclosure_invalid')
            with checkpoint('path_prepare'):
                root = Path(config.get('WORK_ROOT')).resolve()
                root.mkdir(parents=True, exist_ok=True)
                audio_dir = root / 'audio' / reference.attempt_id
                audio_dir.mkdir(parents=True, exist_ok=False)
                audio = audio_dir / 'input.mp3'
            with checkpoint('audio_fetch'):
                dependencies.audio(url, audio, config.audio_timeout)
            with checkpoint('audio_validate'):
                if not audio.is_file() or audio.is_symlink() or audio.stat().st_size == 0:
                    raise RuntimeFailure('audio_missing')
                staging = root / 'candidates'
                staging.mkdir(exist_ok=True)
            with checkpoint('transcriber_init'):
                if transcriber is None:
                    transcriber = dependencies.transcriber(model_size='small', device='cpu', compute_type='int8')
            local = AttemptReference(reference.observation.with_audio_path(audio), reference.attempt_id)
            result = build_candidate(transcriber, reference=local, staging_root=staging,
                language='zh', initial_prompt='這是一段Podcast對話。請將語音內容準確轉錄為繁體中文。')
            with checkpoint('callback_finalize'):
                candidate = replace(result.candidate, reference=reference) if result.candidate else None
                return replace(result, reference=reference, candidate=candidate)
        except Exception:
            # Preserve the existing public failure/reason boundary. Stage metadata
            # stays in the attempt context, including for immutable exceptions.
            raise RuntimeFailure('production_work_failed') from None
    def upload(candidate):
        context = current_execution()
        if context is None:
            return upload_candidate(service, candidate=candidate,
                                    target_folder_id=config.get('DRIVE_ARTIFACT_FOLDER_ID'))
        # Observe the accepted uploader without changing its snapshot, request,
        # verification or stop-on-first-failure semantics. It advances to JSON
        # only after TXT is verified, and returns fixed per-artifact outcomes.
        stages = ('candidate_upload_txt', 'candidate_upload_json')
        class ObservedFiles:
            count = 0

            def create(self, **kwargs):
                if self.count == 1:
                    context.event(stages[0], 'COMPLETE')
                    context.event(stages[1], 'START')
                self.count += 1
                return service.files().create(**kwargs)

            def get_media(self, **kwargs):
                return service.files().get_media(**kwargs)

        files = ObservedFiles()
        class ObservedService:
            def files(self):
                return files

        context.event(stages[0], 'START')
        try:
            result = upload_candidate(ObservedService(), candidate=candidate,
                target_folder_id=config.get('DRIVE_ARTIFACT_FOLDER_ID'))
        except Exception:
            context.event(stages[min(files.count, 1)], 'FAIL')
            context.event(stages[0], 'FAIL')
            raise
        for stage, artifact in zip(stages, result.artifacts):
            if artifact.write_outcome == 'not_attempted':
                continue
            if stage not in context.active and stage not in context.failed:
                # Pre-request preparation/local-integrity failure.
                if stage == stages[1] and files.count < 2:
                    context.event(stages[1], 'START')
            context.event(stage, 'COMPLETE' if artifact.write_outcome == 'success'
                          and artifact.verification_outcome == 'success' else 'FAIL')
        # A preflight JSON failure prevents the pending TXT operation too.
        if stages[0] in context.active:
            context.event(stages[0], 'FAIL')
        return result
    return dict(coordinator_factory=coordinator, discover=discover, work=work, upload=upload,
                cutover_at=config.cutover_at, feed_namespace=config.get('FEED_NAMESPACE'))


def run(environ=None, *, dependencies=None):
    config = Config.from_env(os.environ if environ is None else environ)
    import main as application
    return application.run_offline_pipeline(**compose(config, dependencies or Dependencies()))


def exit_status(report):
    if any(f.get('reason') == 'initialization_required' or f.get('phase') == 'configuration'
           for f in report.run_errors):
        return 1
    return 2 if report.failed or report.pending or report.run_errors else 0


def main(environ=None, *, dependencies=None):
    try:
        report = run(environ, dependencies=dependencies)
    except RuntimeFailure as error:
        # Only our fixed public reason codes reach the console.
        reason = str(error)
        if not (reason.startswith('config_invalid:PODCAST_') and reason.split(':', 1)[1]
                in (*REQUIRED, 'PODCAST_AUTH_TIMING_POLICY')):
            reason = 'production_startup_failed'
        print(reason)
        return 1
    except Exception:
        print('production_startup_failed')
        return 1
    if any(f.get('reason') == 'initialization_required' for f in report.run_errors):
        print('initialization_required')
    return exit_status(report)


if __name__ == '__main__':
    raise SystemExit(main())
