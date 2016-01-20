from django.db import models
from django.conf import settings
from ci.gitlab import api as gitlab_api
from ci.gitlab import oauth as gitlab_auth
from ci.bitbucket import api as bitbucket_api
from ci.bitbucket import oauth as bitbucket_auth
from ci.github import api as github_api
from ci.github import oauth as github_auth
import random, re
from datetime import timedelta

class DBException(Exception):
  pass

class JobStatus(object):
  NOT_STARTED = 0
  SUCCESS = 1
  RUNNING = 2
  FAILED = 3
  FAILED_OK = 4
  CANCELED = 5
  ACTIVATION_REQUIRED = 6

  STATUS_CHOICES = ((NOT_STARTED, "Not started"),
      (SUCCESS, "Passed"),
      (RUNNING, "Running"),
      (FAILED, "Failed"),
      (FAILED_OK, "Allowed to fail"),
      (CANCELED, "Canceled by user"),
      (ACTIVATION_REQUIRED, "Requires activation"),
      )
  SHORT_CHOICES = (
      (NOT_STARTED, "Not_Started"),
      (SUCCESS, 'Passed'),
      (RUNNING, 'Running'),
      (FAILED, 'Failed'),
      (FAILED_OK, 'Failed_OK'),
      (CANCELED, 'Canceled'),
      (ACTIVATION_REQUIRED, 'Activation_Required'),
      )

  @staticmethod
  def to_str(status):
    return JobStatus.STATUS_CHOICES[status][1]

  @staticmethod
  def to_slug(status):
    return JobStatus.SHORT_CHOICES[status][1]

class GitServer(models.Model):
  """
  One of the git servers. The type for one of the main
  servers. The server could be hosted internally though,
  like our private GitLab server.
  """

  SERVER_TYPE = ((settings.GITSERVER_GITHUB, "GitHub"),
      (settings.GITSERVER_GITLAB, "GitLab"),
      (settings.GITSERVER_BITBUCKET, "BitBucket"),
      )
  name = models.CharField(max_length=120)
  base_url = models.URLField() # base url for checking things out
  host_type = models.IntegerField(choices=SERVER_TYPE, unique=True)

  def __unicode__(self):
    return self.name

  def api(self):
    if self.host_type == settings.GITSERVER_GITHUB:
      return github_api.GitHubAPI()
    elif self.host_type == settings.GITSERVER_GITLAB:
      return gitlab_api.GitLabAPI()
    elif self.host_type == settings.GITSERVER_BITBUCKET:
      return bitbucket_api.BitBucketAPI()

  def auth(self):
    if self.host_type == settings.GITSERVER_GITHUB:
      return github_auth.GitHubAuth()
    elif self.host_type == settings.GITSERVER_GITLAB:
      return gitlab_auth.GitLabAuth()
    elif self.host_type == settings.GITSERVER_BITBUCKET:
      return bitbucket_auth.BitBucketAuth()

def generate_build_key():
  return random.SystemRandom().randint(0, 2000000000)

class GitUser(models.Model):
  """
  A user that will be signed into the system via
  one of the supported git servers. The username is
  the username on the the server (like GitHub).
  The build_key gets autogenerated and is intented
  to prevent outside users from accessing certain
  endpoints.
  """

  name = models.CharField(max_length=120)
  build_key = models.IntegerField(default=generate_build_key, unique=True)
  server = models.ForeignKey(GitServer, related_name='users')
  token = models.CharField(max_length=1024, blank=True) # holds json encoded token

  def __unicode__(self):
    return self.name

  def start_session(self):
    return self.server.auth().start_session_for_user(self)

  def api(self):
    return self.server.api()

  class Meta:
    unique_together = ['name', 'server']
    ordering = ['name']


class Repository(models.Model):
  """
  For use in repositories on GitHub, etc. A typical structure is <base>/<repo>.
  It will be a username or organization name.
  """
  name = models.CharField(max_length=120)
  user = models.ForeignKey(GitUser, related_name='repositories')

  def __unicode__(self):
    return "%s/%s" % (self.user.name, self.name)

  def url(self):
    server = self.user.server
    return server.api().repo_url(self.user.name, self.name)

  class Meta:
    unique_together = ['user', 'name']

class Branch(models.Model):
  """
  A branch of a repository.
  """
  name = models.CharField(max_length=120)
  repository = models.ForeignKey(Repository, related_name='branches')
  status = models.IntegerField(choices=JobStatus.STATUS_CHOICES, default=JobStatus.NOT_STARTED)
  last_modified = models.DateTimeField(auto_now=True)

  def __unicode__(self):
    return "{}:{}".format( str(self.repository), self.name)

  def user(self):
    return self.repository.user

  def server(self):
    return self.repository.user.server

  def status_slug(self):
    return JobStatus.to_slug(self.status)

  class Meta:
    unique_together = ['name', 'repository']

class Commit(models.Model):
  """
  A particular commit in the git repository, identified by the hash.
  """
  branch = models.ForeignKey(Branch, related_name='commits')
  sha = models.CharField(max_length=120)
  ssh_url = models.URLField(blank=True)

  def __unicode__(self):
      return "{}:{}".format(str(self.branch), self.sha[:6])

  class Meta:
    unique_together = ['branch', 'sha']

  def server(self):
    return self.branch.repository.user.server

  def user(self):
    return self.branch.repository.user

  def repo(self):
    return self.branch.repository

  def url(self):
    repo = self.repo()
    user = repo.user
    server = user.server
    return server.api().commit_html_url(user.name, repo.name, self.sha)

class PullRequest(models.Model):
  """
  A pull request that was generated on a forked repository.
  """
  number = models.IntegerField()
  repository = models.ForeignKey(Repository, related_name='pull_requests')
  title = models.CharField(max_length=120)
  url = models.URLField()
  closed = models.BooleanField(default=False)
  created = models.DateTimeField(auto_now_add=True)
  status = models.IntegerField(choices=JobStatus.STATUS_CHOICES, default=JobStatus.NOT_STARTED)
  last_modified = models.DateTimeField(auto_now=True)

  def __unicode__(self):
    return u'#{} : {}'.format(self.number, self.title)

  class Meta:
    get_latest_by = 'last_modified'
    ordering = ['repository', 'number']
    unique_together = ['repository', 'number']

  def status_slug(self):
    return JobStatus.to_slug(self.status)


def sorted_job_compare(j1, j2):
  """
  Used to sort the jobs in an event group.
  Sort by priorty first then just by name.
  """
  if j1.recipe.priority < j2.recipe.priority:
    return 1
  elif j1.recipe.priority > j2.recipe.priority:
    return -1
  elif j1.recipe.display_name < j2.recipe.display_name:
    return -1
  elif j1.recipe.display_name > j2.recipe.display_name:
    return 1
  else:
    return 0

class Event(models.Model):
  """
  Represents an event that has happened. For pull request and push, it
  relies on the webhook of the repo server (like GitHub). This function will
  then generate the event. It can also be a manually scheduled event that
  just takes the current status of the branch and creates an Event off of that.
  Jobs will be generated off of this table.
  """
  PULL_REQUEST = 0
  PUSH = 1
  MANUAL = 2
  CAUSE_CHOICES = ((PULL_REQUEST, 'Pull request'),
      (PUSH, 'Push'),
      (MANUAL, 'Manual')
      )
  description = models.CharField(max_length=200, default='', blank=True)
  trigger_user = models.CharField(max_length=200, default='', blank=True) # the user who initiated the event
  build_user = models.ForeignKey(GitUser, related_name='events') #the user associated with the build key
  head = models.ForeignKey(Commit, related_name='event_head')
  base = models.ForeignKey(Commit, related_name='event_base')
  status = models.IntegerField(choices=JobStatus.STATUS_CHOICES, default=JobStatus.NOT_STARTED)
  complete = models.BooleanField(default=False)
  cause = models.IntegerField(choices=CAUSE_CHOICES, default=PULL_REQUEST)
  comments_url = models.URLField(null=True, blank=True)
  pull_request = models.ForeignKey(PullRequest, null=True, blank=True, related_name='events')
  # stores the actual json that gets sent from the server to create this event
  json_data = models.TextField(blank=True)

  last_modified = models.DateTimeField(auto_now=True)
  created = models.DateTimeField(db_index=True, auto_now_add=True)

  def __unicode__(self):
    return u'{} : {}'.format(self.CAUSE_CHOICES[self.cause][1], str(self.head) )

  class Meta:
    ordering = ['-created']
    get_latest_by = 'last_modified'
    unique_together = ['build_user', 'head', 'base']

  def cause_str(self):
    if self.PUSH == self.cause:
      return 'Push {}'.format(self.base.branch.name)

    return self.CAUSE_CHOICES[self.cause][1]

  def is_manual(self):
    return self.MANUAL == self.cause

  def status_slug(self):
    return JobStatus.to_slug(self.status)

  def user(self):
    return self.head.user()

  def get_sorted_jobs(self):
    jobs = []
    jobs_set = set()
    other = []
    job_groups = []
    for job in self.jobs.all():
      if job.recipe.dependencies.count() == 0:
        jobs.append(job)
        jobs_set.add(job.recipe)
      else:
        other.append(job)
    # a job has a dependency, but the dependency
    # may not be in the list yet.
    job_groups.append(sorted(jobs[:], cmp=sorted_job_compare))
    while other:
      new_other = []
      new_group = []
      jobs_set = set([j.recipe for j in jobs])
      for job in other:
        depend_set = set([ x for x in job.recipe.dependencies.all()])
        if depend_set.issubset(jobs_set):
          # all depends have been added
          jobs.append(job)
          new_group.append(job)
        else:
          new_other.append(job)
      other = new_other
      job_groups.append(sorted(new_group, cmp=sorted_job_compare))

    return job_groups

class BuildConfig(models.Model):
  """
  Different names for build configurations.
  Used by the client to match available jobs to what
  configurations it supports.
  """
  name = models.CharField(max_length=120)

  def __unicode__(self):
    return self.name

class Recipe(models.Model):
  MANUAL = 0
  AUTO_FOR_AUTHORIZED = 1
  FULL_AUTO = 2
  AUTO_CHOICES = ((MANUAL, "Manual"),
      (AUTO_FOR_AUTHORIZED, "Authorized users"),
      (FULL_AUTO, "Automatic")
      )

  CAUSE_PULL_REQUEST = 0
  CAUSE_PUSH = 1
  CAUSE_MANUAL = 2
  CAUSE_CHOICES = ((CAUSE_PULL_REQUEST, 'Pull request'),
      (CAUSE_PUSH, 'Push'),
      (CAUSE_MANUAL, 'Manual')
      )
  name = models.CharField(max_length=120)
  display_name = models.CharField(max_length=120)
  creator = models.ForeignKey(GitUser, related_name='recipes')
  repository = models.ForeignKey(Repository, related_name='recipes')
  branch = models.ForeignKey(Branch, null=True, blank=True, related_name='recipes')
  abort_on_failure = models.BooleanField(default=True)
  private = models.BooleanField(default=False)
  active = models.BooleanField(default=True)
  cause = models.IntegerField(choices=CAUSE_CHOICES, default=CAUSE_PULL_REQUEST)
  build_configs = models.ManyToManyField(BuildConfig)
  auto_authorized = models.ManyToManyField(GitUser, related_name='auto_authorized', blank=True)
  # dependencies depend on other recipes which means that it isn't symmetrical
  dependencies = models.ManyToManyField('self', through='RecipeDependency', symmetrical=False, blank=True)
  automatic = models.IntegerField(choices=AUTO_CHOICES, default=FULL_AUTO)
  priority = models.PositiveIntegerField(default=0)
  last_modified = models.DateTimeField(auto_now=True)
  created = models.DateTimeField(auto_now_add=True)

  def __unicode__(self):
    return self.name

  class Meta:
    get_latest_by = 'last_modified'

  def cause_str(self):
    if self.CAUSE_PUSH == self.cause:
      return 'Push {}'.format(self.branch.name)

    return self.CAUSE_CHOICES[self.cause][1]

  def configs_str(self):
    return ', '.join([ config.name for config in self.build_configs.all() ])

  def dependency_str(self):
    return ', '.join([ dep.display_name for dep in self.dependencies.all() ])

  def auto_str(self):
    return self.AUTO_CHOICES[self.automatic][1]

class RecipeDependency(models.Model):
  recipe = models.ForeignKey(Recipe)
  dependency = models.ForeignKey(Recipe, related_name='all_dependencies')
  abort_on_failure = models.BooleanField(default=True)

  def __unicode__(self):
    return u'{}->{}'.format(self.recipe.name, self.dependency.name)

  class Meta:
    unique_together = ['recipe', 'dependency']

class RecipeEnvironment(models.Model):
  """
  Name value pairs to be inserted into the environment
  at the recipe level, available to all steps.
  """
  recipe = models.ForeignKey(Recipe, related_name='environment_vars')
  name = models.CharField(max_length=120)
  value = models.CharField(max_length=120)

  def __unicode__(self):
    return u'{}={}'.format( self.name, self.value )

class PreStepSource(models.Model):
  """
  Since we use bash to execute our steps, we can just add some
  files to be sourced to import variables, functions, etc, before
  running the step.
  """

  recipe = models.ForeignKey(Recipe, related_name='prestepsources')
  filename = models.CharField(max_length=120, blank=True)

  def __unicode__(self):
    return self.filename


class Step(models.Model):
  """
  A specific step in a recipe. The filename points to a specific script
  that will be executed by the client.
  abort_on_failure: If the test fails and this is true then the job stops and fails. If false then it will continue to the next step
  allowed_to_fail: If this is true and the step fails then the step is marked as FAILED_OK rather than FAIL
  """
  recipe = models.ForeignKey(Recipe, related_name='steps')
  name = models.CharField(max_length=120)
  filename = models.CharField(max_length=120)
  position = models.PositiveIntegerField(default=0)
  abort_on_failure = models.BooleanField(default=True)
  allowed_to_fail = models.BooleanField(default=False)

  def __unicode__(self):
    return self.name

  class Meta:
    ordering = ['position',]

class StepEnvironment(models.Model):
  """
  Name value pairs to be inserted into the environment
  before running each step. Only available to a step.
  """
  step = models.ForeignKey(Step, related_name='step_environment')
  name = models.CharField(max_length=120)
  value = models.CharField(max_length=120)

  def __unicode__(self):
    return u'{}:{}'.format( self.name, self.value )

class Client(models.Model):
  """
  Represents a client that is run on the build servers. Since the
  client polls the web server while it is running or if it is idle,
  we can keep track of its status.
  """
  RUNNING = 0
  IDLE = 1
  DOWN = 2
  STATUS_CHOICES = ((RUNNING, "Running a job"),
      (IDLE, "Looking for work"),
      (DOWN, "Not active")
      )
  name = models.CharField(max_length=120)
  ip = models.GenericIPAddressField()
  status = models.IntegerField(choices=STATUS_CHOICES, default=DOWN)
  status_message = models.CharField(max_length=120, blank=True)
  last_seen = models.DateTimeField(auto_now=True)
  created = models.DateTimeField(auto_now_add=True)

  def __unicode__(self):
    return self.name

  def status_str(self):
    return self.STATUS_CHOICES[self.status][1]

  class Meta:
    get_latest_by = 'last_seen'


class Job(models.Model):
  """
  Represents the execution of a single config of a Recipe.
  """
  recipe = models.ForeignKey(Recipe, related_name='jobs')
  event = models.ForeignKey(Event, related_name='jobs')
  client = models.ForeignKey(Client, null=True, blank=True)
  complete = models.BooleanField(default=False)
  invalidated = models.BooleanField(default=False)
  same_client = models.BooleanField(default=False)
  # ready means that the job can go out for execution.
  ready = models.BooleanField(default=False)
  active = models.BooleanField(default=True)
  config = models.ForeignKey(BuildConfig, related_name='jobs')
  status = models.IntegerField(choices=JobStatus.STATUS_CHOICES, default=JobStatus.NOT_STARTED)
  seconds = models.DurationField(default=timedelta)
  last_modified = models.DateTimeField(auto_now=True)
  created = models.DateTimeField(auto_now_add=True)

  def __unicode__(self):
    return u'{}:{}'.format(self.recipe.name, self.config.name)

  def status_slug(self):
    if not self.active and self.status == JobStatus.NOT_STARTED:
      return JobStatus.to_slug(JobStatus.ACTIVATION_REQUIRED)
    return JobStatus.to_slug(self.status)

  def status_str(self):
    if not self.active and self.status == JobStatus.NOT_STARTED:
      return JobStatus.to_str(JobStatus.ACTIVATION_REQUIRED)
    return JobStatus.to_str(self.status)

  def active_results(self):
    return self.step_results.exclude(status=JobStatus.NOT_STARTED)

  def failed(self):
    return self.status == JobStatus.FAILED or self.status == JobStatus.FAILED_OK

  def failed_result(self):
    if self.failed():
      result = self.step_results.filter(status__in=[JobStatus.FAILED, JobStatus.FAILED_OK]).order_by('status', 'last_modified').first()
      return result
    return None

  class Meta:
    ordering = ["-last_modified"]
    get_latest_by = 'last_modified'
    unique_together = ['recipe', 'event']


def html_color_string(matchobj):
  color_code = matchobj.group(2)
  if color_code == '39' or color_code == '0':
    return '</span>'
  else:
    return '<span class="term-fg' + color_code + '">'

def terminalize_output(output):
  # Replace "<" signs
  output = output.replace("&", "&amp;")
  output = output.replace("<", "&lt;")
  output = output.replace("\n", "<br/>")
  '''
     Substitute terminal color codes for CSS tags.
     The bold tag can be a modifier on another tag
     and thus sometimes doesn't have its own
     closing tag. Just ignore it ini that case.
  '''
  return re.sub("(\33\[1m)*\33\[(\d{1,2})m", html_color_string, output)

class StepResult(models.Model):
  """
  The result of a single step of a Recipe for a single Job.
  """
  job = models.ForeignKey(Job, related_name='step_results')
  # replicate some of the Step fields because if someone changes
  # the recipe then it wouldn't be represented of the actual
  # results. So these will just be copied over when the result
  # is created.
  name = models.CharField(max_length=120, blank=True, default='')
  filename = models.CharField(max_length=120, blank=True, default='')
  position = models.PositiveIntegerField(default=0)
  abort_on_failure = models.BooleanField(default=True)
  allowed_to_fail = models.BooleanField(default=False)

  exit_status = models.IntegerField(default=0) # return value of the script
  status = models.IntegerField(choices=JobStatus.STATUS_CHOICES, default=JobStatus.NOT_STARTED)
  complete = models.BooleanField(default=False)
  output = models.TextField(blank=True) # output of the step
  seconds = models.DurationField(default=timedelta) #run time
  last_modified = models.DateTimeField(auto_now=True)

  def __unicode__(self):
    return u'{}:{}'.format(self.job, self.name)

  class Meta:
    unique_together = ['job', 'position']
    ordering = ['position',]

  def status_slug(self):
    return JobStatus.to_slug(self.status)

  def clean_output(self):
    return terminalize_output(self.output)

