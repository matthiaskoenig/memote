# -*- coding: utf-8 -*-

# Copyright 2017 Novo Nordisk Foundation Center for Biosustainability,
# Technical University of Denmark.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Provide commands for running the test suite on a metabolic model."""

from __future__ import absolute_import

import io
import os
import sys
import logging
from functools import partial
from gzip import GzipFile
from os.path import join, isfile
from multiprocessing import Process
from getpass import getpass
from time import sleep
from tempfile import mkdtemp
from shutil import copy2, move

import click
import click_log
import git
import ruamel.yaml as yaml
from cobra.io import read_sbml_model
from cookiecutter.main import cookiecutter, get_user_config
from github import (
    Github, BadCredentialsException, UnknownObjectException, GithubException)
from sqlalchemy import create_engine
from sqlalchemy.exc import ArgumentError
from travispy import TravisPy
from travispy.errors import TravisError
from travis.encrypt import encrypt_key, retrieve_public_key

import memote.suite.api as api
import memote.suite.cli.callbacks as callbacks
from memote import __version__
from memote.suite.cli import CONTEXT_SETTINGS
from memote.suite.cli.reports import report
from memote.suite.results import (
    ResultManager, RepoResultManager, SQLResultManager, HistoryManager)

LOGGER = logging.getLogger()
click_log.basic_config(LOGGER)


@click.group()
@click.help_option("--help", "-h")
@click.version_option(__version__, "--version", "-V")
@click_log.simple_verbosity_option(
    LOGGER, default="INFO", show_default=True,
    type=click.Choice(["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"]))
def cli():
    """
    Metabolic model testing command line tool.

    In its basic invocation memote runs a test suite on a metabolic model.
    Through various subcommands it can further generate three types of pretty
    HTML reports (snapshot, diff and history), generate a model repository
    structure for starting a new project, and recreate the test result history.

    """
    pass


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.help_option("--help", "-h")
@click.option("--collect/--no-collect", default=True, show_default=True,
              callback=callbacks.validate_collect,
              help="Whether or not to collect test data needed for "
                   "generating a report.")
@click.option("--filename", type=click.Path(exists=False, writable=True),
              default="result.json", show_default=True,
              help="Path for the collected results as JSON.")
@click.option("--location", envvar="MEMOTE_LOCATION",
              help="If invoked inside a git repository, try to interpret "
              "the string as an rfc1738 compatible database URL which will be "
              "used to store the test results. Otherwise write to this "
              "directory using the commit hash as the filename.")
@click.option("--ignore-git", is_flag=True, show_default=True,
              help="Avoid checking the git repository status.")
@click.option("--pytest-args", "-a", callback=callbacks.validate_pytest_args,
              help="Any additional arguments you want to pass to pytest. "
                   "Should be given as one continuous string in quotes.")
@click.option("--exclusive", multiple=True, metavar="TEST",
              help="The name of a test or test module to be run exclusively. "
                   "All other tests are skipped. This option can be used "
                   "multiple times and takes precedence over '--skip'.")
@click.option("--skip", multiple=True, metavar="TEST",
              help="The name of a test or test module to be skipped. This "
                   "option can be used multiple times.")
@click.option("--solver", type=click.Choice(["cplex", "glpk", "gurobi"]),
              default="glpk", show_default=True,
              help="Set the solver to be used.")
@click.option("--experimental", type=click.Path(exists=True, dir_okay=False),
              default=None, callback=callbacks.validate_experimental,
              help="Define additional tests using experimental data.")
@click.option("--custom-tests", type=click.Path(exists=True, file_okay=False),
              multiple=True,
              help="A path to a directory containing custom test "
                   "modules. Please refer to the documentation for more "
                   "information on how to write custom tests "
                   "(memote.readthedocs.io). This option can be specified "
                   "multiple times.")
@click.argument("model", type=click.Path(exists=True, dir_okay=False),
                envvar="MEMOTE_MODEL", callback=callbacks.validate_model)
def run(model, collect, filename, location, ignore_git, pytest_args, exclusive,
        skip, solver, experimental, custom_tests):
    """
    Run the test suite on a single model and collect results.

    MODEL: Path to model file. Can also be supplied via the environment variable
    MEMOTE_MODEL or configured in 'setup.cfg' or 'memote.ini'.

    """
    if ignore_git:
        repo = None
    else:
        repo = callbacks.probe_git()
    if not any(a.startswith("--tb") for a in pytest_args):
        pytest_args = ["--tb", "short"] + pytest_args
    if not any(a.startswith("-v") for a in pytest_args):
        pytest_args.append("-vv")
    # Add further directories to search for tests.
    pytest_args.extend(custom_tests)
    model.solver = solver
    if collect:
        if repo is None:
            manager = ResultManager()
            store = partial(manager.store, filename=filename)
        else:
            if location is None:
                LOGGER.critical(
                    "Working with a repository requires a storage location.")
                sys.exit(1)
            try:
                manager = SQLResultManager(repository=repo, location=location)
            except (AttributeError, ArgumentError):
                manager = RepoResultManager(repository=repo, location=location)
            store = manager.store
    code, result = api.test_model(
        model=model, results=True, pytest_args=pytest_args, skip=skip,
        exclusive=exclusive, experimental=experimental)
    if collect:
        store(result=result)
    sys.exit(code)


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.help_option("--help", "-h")
@click.option("--replay", is_flag=True,
              help="Create a memote repository using the exact same answers "
              "as before. This will not overwrite existing directories. If "
              "you want to adjust the answers, edit the template "
              "'{}'.".format(join(get_user_config()["replay_dir"],
                                  "cookiecutter-memote.json")))
@click.option("--directory", type=click.Path(exists=True, file_okay=False,
                                             writable=True),
              help="Create a new model repository in the given directory.")
def new(directory, replay):
    """
    Create a suitable model repository structure from a template.

    By using a cookiecutter template, memote will ask you a couple of questions
    and set up a new directory structure that will make your life easier. The
    new directory will be placed in the current directory or respect the given
    --directory option.

    """
    if directory is None:
        directory = os.getcwd()
    cookiecutter("gh:opencobra/cookiecutter-memote", output_dir=directory,
                 replay=replay)


def _model_from_stream(stream, filename):
    if filename.endswith(".gz"):
        with GzipFile(fileobj=stream) as file_handle:
            model = read_sbml_model(file_handle)
    else:
        model = read_sbml_model(stream)
    return model


def _test_history(model, solver, manager, commit, pytest_args, skip,
                  exclusive, experimental):
    model.solver = solver
    _, result = api.test_model(
        model, results=True, pytest_args=pytest_args, skip=skip,
        exclusive=exclusive, experimental=experimental)
    manager.store(result, commit=commit)


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.help_option("--help", "-h")
@click.option("--rewrite/--no-rewrite", default=False,
              help="Whether to overwrite existing results.")
@click.option("--location", envvar="MEMOTE_LOCATION",
              help="Location of test results. Can either by a directory or an "
                   "rfc1738 compatible database URL.")
@click.option("--pytest-args", "-a", callback=callbacks.validate_pytest_args,
              help="Any additional arguments you want to pass to pytest. "
                   "Should be given as one continuous string.")
@click.option("--deployment", default="gh-pages", show_default=True,
              help="Results will be read from and committed to the given "
                   "branch.")
@click.option("--solver", type=click.Choice(["cplex", "glpk", "gurobi"]),
              default="glpk", show_default=True,
              help="Set the solver to be used.")
@click.option("--exclusive", multiple=True, metavar="TEST",
              help="The name of a test or test module to be run exclusively. "
                   "All other tests are skipped. This option can be used "
                   "multiple times and takes precedence over '--skip'.")
@click.option("--skip", multiple=True, metavar="TEST",
              help="The name of a test or test module to be skipped. This "
                   "option can be used multiple times.")
@click.argument("model", type=click.Path(exists=False, dir_okay=False),
                envvar="MEMOTE_MODEL")
@click.argument("message")
@click.argument("commits", metavar="[COMMIT] ...", nargs=-1)
def history(model, message, rewrite, solver, location, pytest_args, deployment,
            commits, skip, exclusive, experimental=None):  # noqa: D301
    """
    Re-compute test results for the git branch history.

    MESSAGE is a commit message in case results were modified or added.

    There are two distinct modes:

    \b
    1. Completely re-compute test results for each commit in the git history.
       This should only be necessary when memote is first used with existing
       model repositories.
    2. By giving memote specific commit hashes, it will re-compute test results
       for those only.

    """
    if model is None:
        raise click.BadParameter("No 'model' path given or configured.")
    if location is None:
        raise click.BadParameter("No 'location' given or configured.")
    if "--tb" not in pytest_args:
        pytest_args = ["--tb", "no"] + pytest_args
    try:
        repo = git.Repo()
    except git.InvalidGitRepositoryError:
        LOGGER.critical(
            "The history requires a git repository in order to follow "
            "the model's commit history.")
        sys.exit(1)
    previous = repo.active_branch
    # Temporarily move the results to a new location so that they are
    # available while checking out the various commits.
    repo.heads[deployment].checkout()
    engine = None
    tmp_location = mkdtemp()
    try:
        # Test if the location can be opened as a database.
        engine = create_engine(location)
        engine.dispose()
        new_location = location
        if location.startswith("sqlite"):
            # Copy the SQLite database to a temporary location. Other
            # databases are not file-based and thus git independent.
            url = location.split("/", maxsplit=3)
            if isfile(url[3]):
                copy2(url[3], tmp_location)
            new_location = "{}/{}".format(
                "/".join(url[:3] + [tmp_location]), url[3])
            LOGGER.info("Temporarily moving database from '%s' to '%s'.",
                        url[3], join(tmp_location, url[3]))
        manager = SQLResultManager(repository=repo, location=new_location)
    except (AttributeError, ArgumentError):
        LOGGER.info("Temporarily moving results from '%s' to '%s'.",
                    location, tmp_location)
        move(location, tmp_location)
        new_location = join(tmp_location, location)
        manager = RepoResultManager(repository=repo, location=new_location)
    history = HistoryManager(repository=repo, manager=manager)
    history.load_history(model, skip={deployment})
    if len(commits) == 0:
        commits = list(history.iter_commits())
    for commit in commits:
        cmt = repo.commit(commit)
        # Rewrite to full length hexsha.
        commit = cmt.hexsha
        if model not in cmt.stats.files:
            LOGGER.info(
                "The model was not modified in commit '{}'. "
                "Skipping.".format(commit))
            continue
        # Should we overwrite an existing result?
        if commit in history and not rewrite:
            LOGGER.info(
                "Result for commit '{}' exists. Skipping.".format(commit))
            continue
        LOGGER.info(
            "Running the test suite for commit '{}'.".format(commit))
        blob = cmt.tree[model]
        model_obj = _model_from_stream(blob.data_stream, blob.name)
        proc = Process(
            target=_test_history,
            args=(model_obj, solver, manager, commit, pytest_args, skip,
                  exclusive, experimental))
        proc.start()
        proc.join()
    # Copy back all new and modified files and add them to the index.
    repo.heads[deployment].checkout()
    if engine is not None:
        manager.session.close()
        if location.startswith("sqlite"):
            copy2(join(tmp_location, url[3]), url[3])
    else:
        move(new_location, os.getcwd())
    repo.git.add(".")
    repo.index.commit(message)
    # Checkout the original branch.
    previous.checkout()
    LOGGER.info("Done.")


@cli.command(context_settings=CONTEXT_SETTINGS)
@click.help_option("--help", "-h")
@click.option("--note", default="memote-ci access", show_default=True,
              help="A note describing a personal access token on GitHub. "
                   "Must be unique!")
@click.option("--github_repository", callback=callbacks.validate_repository,
              help="The repository name on GitHub. Usually this is configured "
                   "for you.")
@click.option("--github_username", callback=callbacks.validate_username,
              help="The GitHub username. Usually this is configured for you.")
def online(note, github_repository, github_username):
    """Upload the repository to GitHub and enable testing on Travis CI."""
    try:
        repo = git.Repo()
    except git.InvalidGitRepositoryError:
        LOGGER.critical(
            "'memote online' requires a git repository in order to follow "
            "the current branch's commit history.")
        sys.exit(1)
    password = getpass("GitHub Password: ")
    gh = Github(github_username, password)
    user = gh.get_user()
    try:
        when = user.created_at.isoformat(sep=" ")
        LOGGER.info(
            "Logged in to user '{}' created on '{}'.".format(user.login, when))
    except BadCredentialsException:
        LOGGER.critical("Incorrect username or password!")
        sys.exit(1)
    try:
        gh_repo = user.get_repo(github_repository)
        LOGGER.warning(
            "Using existing repository '{}'. This may override previous "
            "settings.".format(github_repository))
    except UnknownObjectException:
        gh_repo = user.create_repo(github_repository)
    if note == "memote-ci access":
        note = "{} to {}".format(note, github_repository)
    try:
        LOGGER.info("Creating token.")
        auth = user.create_authorization(scopes=["repo"], note=note)
    except GithubException:
        LOGGER.critical(
            "A personal access token with the note '{}' already exists. "
            "Either delete it or choose another note using the option "
            "'--note'.".format(note))
        sys.exit(1)
    try:
        LOGGER.info("Authorizing with TravisCI.")
        travis = TravisPy.github_auth(auth.token)
        t_user = travis.user()
    except TravisError:
        LOGGER.critical(
            "Something is wrong with the generated token or you did not "
            "link your GitHub account on 'https://travis-ci.org/'!")
        sys.exit(1)
    LOGGER.info("Synchronizing repositories.")
    while not t_user.sync():
        sleep(0.1)
    try:
        t_repo = travis.repo(gh_repo.full_name)
    except TravisError:
        LOGGER.critical(
            "Repository could not be found. Is it on GitHub already and "
            "spelled correctly?")
        sys.exit(1)
    if t_repo.enable():
        LOGGER.info(
            "Your repository is now on GitHub and automatic testing has "
            "been enabled on Travis CI. Congrats!")
    else:
        LOGGER.critical("Unable to enable automatic testing on Travis CI!")
        sys.exit(1)
    LOGGER.info(
        "Encrypting GitHub token for repo '{}'.".format(gh_repo.full_name))
    key = retrieve_public_key(gh_repo.full_name)
    secret = encrypt_key(key, "GITHUB_TOKEN={}".format(auth.token).encode())
    LOGGER.info("Storing GitHub token in '.travis.yml'.")
    with io.open(".travis.yml", "r") as file_h:
        config = yaml.load(file_h, yaml.RoundTripLoader)
    global_env = config.setdefault("env", {}).setdefault("global", [])
    if global_env is None:
        config["env"]["global"] = global_env = []
    global_env.append({"secure": secret})
    with io.open(".travis.yml", "w") as file_h:
        yaml.dump(config, file_h, Dumper=yaml.RoundTripDumper)
    repo.index.add([".travis.yml"])
    repo.index.commit("chore: add encrypted GitHub access token")
    repo.remotes.origin.push(all=True)


cli.add_command(report)
