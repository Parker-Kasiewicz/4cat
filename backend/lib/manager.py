"""
The heart of the app - manages jobs and workers
"""
import signal
import time

from backend import all_modules
from backend.lib.keyboard import KeyPoller
from common.lib.exceptions import JobClaimedException, JobNotFoundException
from common.lib.helpers import get_instance_id
from common.lib.job import Job


class WorkerManager:
	"""
	Manages the job queue and worker pool
	"""
	queue = None
	db = None
	log = None

	worker_pool = {}
	stopping_workers = []
	job_mapping = {}
	pool = []
	looping = True

	def __init__(self, queue, database, logger, as_daemon=True):
		"""
		Initialize manager

		:param queue:  Job queue
		:param database:  Database handler
		:param logger:  Logger object
		:param bool as_daemon:  Whether the manager is being run as a daemon
		"""
		self.queue = queue
		self.db = database
		self.log = logger

		if not as_daemon:
			# listen for input if running interactively
			self.key_poller = KeyPoller(manager=self)
			self.key_poller.start()
		else:
			signal.signal(signal.SIGTERM, self.abort)
			signal.signal(signal.SIGINT, self.request_interrupt)

		self.validate_datasources()
		instance_id = get_instance_id()

		# queue a job for the api handler so it will be run
		self.queue.add_job("api", remote_id=instance_id, instance=instance_id)

		# queue worker that deletes expired datasets every so often
		self.queue.add_job("expire-datasets", remote_id=instance_id, interval=300, instance=instance_id)

		# queue worker that calculates datasource metrics every day
		self.queue.add_job("datasource-metrics", remote_id=instance_id, interval=86400, instance=instance_id)

		# queue worker that cleans up orphaned result files
		self.queue.add_job("clean-temp-files", remote_id=instance_id, interval=3600, instance=instance_id)

		self.log.info('4CAT Started')

		# it's time
		self.loop()

	def delegate(self):
		"""
		Delegate work

		Checks for open jobs, and then passes those to dedicated workers, if
		slots are available for those workers.
		"""
		jobs = self.queue.get_all_jobs(restrict_claimable=False)
		known_job_ids = [j.data["id"] for j in jobs]

		num_active = sum([len(self.worker_pool[jobtype]) for jobtype in self.worker_pool])
		self.log.debug("Running workers: %i" % num_active)

		# clean up workers that have finished processing
		# request interrupts for workers that no longer have a record in the database
		for jobtype in self.worker_pool:
			all_workers = self.worker_pool[jobtype]
			for worker in all_workers:
				if not worker.is_alive():
					worker.join()
					self.worker_pool[jobtype].remove(worker)

					if worker.job.data["id"] in self.stopping_workers:
						# this was stopped via an interrupt
						self.stopping_workers.remove(worker.job.data["id"])

				elif worker.job.data["id"] not in known_job_ids and worker.job.data["id"] not in self.stopping_workers:
					# job has been cancelled in the meantime
					self.log.info("Requesting interrupt for job %s" % worker.job.data["jobtype"])
					worker.request_interrupt()

					# this is so an interrupt isn't requested every loop while
					# the worker is already quitting
					self.stopping_workers.append(worker.job.data["id"])

			del all_workers

		# check if workers are available for unclaimed jobs
		for job in jobs:
			if not job.is_claimable():
				continue

			jobtype = job.data["jobtype"]

			if jobtype in all_modules.workers:
				worker_class = all_modules.workers[jobtype]
				if jobtype not in self.worker_pool:
					self.worker_pool[jobtype] = []

				# if a job is of a known type, and that job type has open
				# worker slots, start a new worker to run it
				if len(self.worker_pool[jobtype]) < worker_class.max_workers:
					try:
						self.log.info("Starting new worker for job %s" % jobtype)
						job.claim()
						worker = worker_class(logger=self.log, manager=self, job=job, modules=all_modules)
						worker.start()
						self.worker_pool[jobtype].append(worker)
					except JobClaimedException:
						# it's fine
						pass

		time.sleep(1)

	def loop(self):
		"""
		Main loop

		Constantly delegates work, until no longer looping, after which all
		workers are asked to stop their work. Once that has happened, the loop
		properly ends.
		"""
		while self.looping:
			self.delegate()

		self.log.info("Telling all workers to stop doing whatever they're doing...")
		for jobtype in self.worker_pool:
			for worker in self.worker_pool[jobtype]:
				if hasattr(worker, "request_interrupt"):
					worker.request_interrupt()
				else:
					worker.abort()

		# wait for all workers to finish
		self.log.info("Waiting for all workers to finish...")
		for jobtype in self.worker_pool:
			for worker in self.worker_pool[jobtype]:
				self.log.info("Waiting for worker %s..." % jobtype)
				worker.join()

		time.sleep(1)

		# abort
		self.log.info("Bye!")

	def validate_datasources(self):
		"""
		Validate data sources

		Logs warnings if not all information is precent for the configured data
		sources.
		"""

		for datasource in all_modules.datasources:
			if datasource + "-search" not in all_modules.workers:
				self.log.error("No search worker defined for datasource %s or its modules are missing. Search queries will not be executed." % datasource)

			all_modules.datasources[datasource]["init"](self.db, self.log, self.queue, datasource)

	def abort(self, signal=None, stack=None):
		"""
		Stop looping the delegator, clean up, and prepare for shutdown
		"""
		self.log.info("Received SIGTERM")

		# cancel all interruptible postgres queries
		# this needs to be done before we stop looping since after that no new
		# jobs will be claimed, and needs to be done here because the worker's
		# own database connection is busy executing the query that it should
		# cancel! so we can't use it to update the job and make it get claimed
		for job in self.queue.get_all_jobs("cancel-pg-query", restrict_claimable=False):
			# this will make all these jobs immediately claimable, i.e. queries
			# will get cancelled asap
			self.log.debug("Cancelling interruptable Postgres queries for connection %s..." % job.data["remote_id"])
			job.claim()
			job.release(delay=0, claim_after=0)

		# wait until all cancel jobs are completed
		while self.queue.get_all_jobs("cancel-pg-query", restrict_claimable=False):
			time.sleep(0.25)

		# now stop looping (i.e. accepting new jobs)
		self.looping = False

	def request_interrupt(self, interrupt_level, job=None, remote_id=None, jobtype=None):
		"""
		Interrupt a job

		This method can be called via e.g. the API, to interrupt a specific
		job's worker. The worker can be targeted either with a Job object or
		with a combination of job type and remote ID, since those uniquely
		identify a job.

		:param int interrupt_level:  Retry later or cancel?
		:param Job job:  Job object to cancel worker for
		:param str remote_id:  Remote ID for worker job to cancel
		:param str jobtype:  Job type for worker job to cancel
		"""

		# find worker for given job
		if job:
			jobtype = job.data["jobtype"]

		if jobtype not in self.worker_pool:
			# no jobs of this type currently known
			return

		for worker in self.worker_pool[jobtype]:
			if (job and worker.job.data["id"] == job.data["id"]) or (worker.job.data["jobtype"] == jobtype and worker.job.data["remote_id"] == remote_id):
				# first cancel any interruptable queries for this job's worker
				while True:
					active_queries = self.queue.get_all_jobs("cancel-pg-query", remote_id=worker.db.appname, restrict_claimable=False)
					if not active_queries:
						# all cancellation jobs have been run
						break

					for cancel_job in active_queries:
						if cancel_job.is_claimed:
							continue

						# this will make the job be run asap
						cancel_job.claim()
						cancel_job.release(delay=0, claim_after=0)

					# give the cancel job a moment to run
					time.sleep(0.25)

				# now all queries are interrupted, formally request an abort
				worker.request_interrupt(interrupt_level)
				return

	def request_delete(self, job=None, remote_id=None, jobtype=None):
		"""
		Delete job from queue

		This will trigger an interrupt to any workers running for this job.
		This can be used to trigger an interrupt regardless of on which
		4CAT instance the job is running - as long as they are working with the
		same database, the interrupt will be triggered on the right instance.

		:param Job job:  Job object to cancel worker for
		:param str remote_id:  Remote ID for worker job to cancel
		:param str jobtype:  Job type for worker job to cancel
		"""
		if not job:
			try:
				job = Job.get_by_remote_ID(remote_id=remote_id, jobtype=jobtype, database=self.db, own_instance_only=False)
			except JobNotFoundException:
				# job doesn't exist - OK, may have been deleted or finished already
				return

		self.log.info(
				"Job deletion requested for job %s/%s/%s" % (job.data["instance"], job.data["jobtype"], job.data["remote_id"]))
		job.finish(delete=True)
