"""
4Chan board scraper - indexes threads and queues them for scraping
"""

from backend.lib.scraper import BasicJSONScraper
from backend.lib.queue import JobAlreadyExistsException


class BoardScraper(BasicJSONScraper):
	"""
	Scrape 4chan boards

	The threads found aren't saved themselves, but new jobs are created to scrape the
	individual threads so post data can be saved
	"""
	type = "board"
	pause = 1  # we're not checking this often, but using claim-after to schedule jobs
	max_workers = 2  # should probably be equivalent to the amount of boards to scrape

	required_fields = ["no", "last_modified"]
	position = 0

	def process(self, data, job):
		"""
		Process scraped board data

		For each thread, a record is inserted into the database if it does not exist yet

		:param dict data: The board data, parsed JSON data
		"""
		new_threads = 0
		for page in data:
			for thread in page["threads"]:
				self.position += 1
				new_threads += self.save_thread(thread, job)

		self.log.info("Board scrape for /%s/ yielded %i new threads" % (job["remote_id"], new_threads))

	def save_thread(self, thread, job):
		"""
		Save thread

		:param dict thread:  Thread data
		:param dict job:  Job data
		:return int:  Number of new threads created (so 0 or 1)
		"""
		# check if we have everything we need
		missing = set(self.required_fields) - set(thread.keys())
		if missing != set():
			self.log.warning("Missing fields %s in scraped thread, ignoring" % repr(missing))
			return False

		thread_data = {
			"id": thread["no"],
			"board": job["remote_id"],
			"index_positions": ""
		}

		# schedule a job for scraping the thread's posts
		try:
			self.queue.add_job(jobtype="thread", remote_id=thread["no"], details={"board": job["remote_id"]})
		except JobAlreadyExistsException:
			# this might happen if the workers can't keep up with the queue
			pass

		# add database record for thread, if none exists yet
		thread_row = self.db.fetchone("SELECT * FROM threads WHERE id = %s", (thread_data["id"],))
		new_thread = 0
		if not thread_row:
			new_thread += 1
			self.db.insert("threads", thread_data)
		elif thread_row["timestamp_deleted"] > 0:
			self.log.warning("Scrape queued for deleted thread %s/%s (deleted at %s)" % (job["remote_id"], thread_data["id"], thread_row["timestamp_deleted"]))

		# update timestamps and position
		position_update = str(self.loop_time) + ":" + str(self.position) + ","
		self.db.execute("UPDATE threads SET timestamp_scraped = %s, timestamp_modified = %s,"
						"index_positions = CONCAT(index_positions, %s) WHERE id = %s",
						(self.loop_time, thread["last_modified"], position_update, thread_data["id"]))

		return new_thread

	def get_url(self):
		"""
		Get URL to scrape for the current job

		:return string: URL to scrape
		"""
		return "http://a.4cdn.org/%s/threads.json" % self.job["remote_id"]
