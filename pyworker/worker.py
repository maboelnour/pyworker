import newrelic.agent

import os, signal, traceback
import time
from contextlib import contextmanager
from pyworker.db import DBConnector
from pyworker.job import Job
from pyworker.logger import Logger
from pyworker.util import get_current_time, get_time_delta

class TimeoutException(Exception): pass
class TerminatedException(Exception): pass

class Worker(object):
    def __init__(self, dbstring, logger=None):
        super(Worker, self).__init__()
        self.logger = Logger(logger)
        self.logger.info('Starting pyworker...')
        self.database = DBConnector(dbstring, self.logger)
        self.sleep_delay = 10
        self.max_attempts = 3
        self.max_run_time = 3600
        self.queue_names = 'default'
        hostname = os.uname()[1]
        pid = os.getpid()
        self.name = 'host:%s pid:%d' % (hostname, pid)

        # Configure NewRelic if ENV variables set
        self.newrelic_app = None
        NEW_RELIC_LICENSE_KEY = os.environ.get("NEW_RELIC_LICENSE_KEY")
        NEW_RELIC_APP_NAME = os.environ.get("NEW_RELIC_APP_NAME")

        # Register Application in NewRelic if configured
        if NEW_RELIC_LICENSE_KEY and NEW_RELIC_APP_NAME:
            self.logger.info('Initializing NewRelic Agent for: %s' % NEW_RELIC_APP_NAME)
            newrelic.agent.initialize()
            self.newrelic_app = newrelic.agent.register_application()

    @contextmanager
    def _time_limit(self, seconds):
        def signal_handler(signum, frame):
            raise TimeoutException(('Execution expired. Either do ' + \
                'the job faster or raise max_run_time > %d seconds') % \
                self.max_run_time)
        signal.signal(signal.SIGALRM, signal_handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)

    @contextmanager
    def _terminatable(self):
        def signal_handler(signum, frame):
            signal_name = 'SIGTERM' if signum == 15 else 'SIGINT'
            self.logger.info('Received signal: %s' % signal_name)
            raise TerminatedException(signal_name)
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        yield

    @contextmanager
    def _instrument(self, job):

        def _latency(job_run_at):
            # we stick to get_current_time() to match the one used in the UPDATE query
            now = get_current_time()

            # Difference between when the job was scheduled `job_run_at`
            # and when the job actually started running `now`
            return (now - job_run_at).total_seconds()

        if self.newrelic_app:
            latency = _latency(job.run_at)

            with newrelic.agent.BackgroundTask(
                    application=self.newrelic_app,
                    name=job.job_name,
                    group='DelayedJob') as task:

                # Record custom attributes for the job transaction
                newrelic.agent.add_custom_attribute('job_id', job.job_id)
                newrelic.agent.add_custom_attribute('job_name', job.job_name)
                newrelic.agent.add_custom_attribute('job_queue', job.queue)
                newrelic.agent.add_custom_attribute('job_latency', latency)
                newrelic.agent.add_custom_attribute('job_attempts', job.attempts)
                # TODO report job.enqueue_attributes if available

                yield task
        else:
            yield

    def run(self):
        # continuously check for new jobs on specified queue from db
        self._cursor = self.database.connect().cursor()
        with self._terminatable():
            while True:
                self.logger.debug('Picking up jobs...')
                job = self.get_job()
                self._current_job = job # used in signal handlers
                try:
                    if job is not None:
                        self.handle_job(job)
                    else: # sleep for a while before checking again for new jobs
                        time.sleep(self.sleep_delay)
                except TerminatedException:
                    break

            self.database.disconnect()

            # If configured shutdown NewRelic Agent to upload data on shutdown
            if self.newrelic_app:
                newrelic.agent.shutdown_agent()

    def get_job(self):
        def get_job_row():
            now = get_current_time()
            expired = now - get_time_delta(seconds=self.max_run_time)
            now, expired = str(now), str(expired)
            queues = self.queue_names.split(',')
            queues = ', '.join(["'%s'" % q for q in queues])
            query = '''
            UPDATE delayed_jobs SET locked_at = '%s', locked_by = '%s'
            WHERE id IN (SELECT delayed_jobs.id FROM delayed_jobs
                WHERE ((run_at <= '%s'
                AND (locked_at IS NULL OR locked_at < '%s')
                OR locked_by = '%s') AND failed_at IS NULL)
                AND delayed_jobs.queue IN (%s)
            ORDER BY priority ASC, run_at ASC LIMIT 1 FOR UPDATE) RETURNING
                id, attempts, run_at, queue, handler
            ''' % (now, self.name, now, expired, self.name, queues)
            self.logger.debug('query: %s' % query)
            self._cursor.execute(query)
            return self._cursor.fetchone()

        job_row = get_job_row()
        if job_row:
            return Job.from_row(job_row, max_attempts=self.max_attempts,
                database=self.database, logger=self.logger)
        else:
            return None

    def handle_job(self, job):
        if job is None:
            return
        with self._instrument(job):
            start_time = time.time()
            error = failed = False
            caught_exception = None
            try:
                if job.abstract:
                    raise ValueError(('Unsupported Job: %s, please import it ' \
                        + 'before you can handle it') % job.class_name)
                else:
                    self.logger.info('Running Job %d' % job.job_id)
                    with self._time_limit(self.max_run_time):
                        job.before()
                        job.run()
                        job.after()
                    job.success()
                    job.remove()
            except Exception as exception:
                error = True
                caught_exception = exception
                # handle error
                error_str = traceback.format_exc()
                failed = job.set_error_unlock(error_str)
                # if that was a termination error, bubble up to caller
                if type(exception) == TerminatedException:
                    raise exception
            finally:
                # report error status
                if self.newrelic_app:
                    newrelic.agent.add_custom_attribute('error', error)
                    newrelic.agent.add_custom_attribute('job_failure', failed)
                    if caught_exception:
                        newrelic.agent.record_exception(caught_exception)
                time_diff = time.time() - start_time
                self.logger.info('Job %d finished in %d seconds' % \
                    (job.job_id, time_diff))
