from __future__ import annotations
from typing import List
import time
import logging
import random
import os
from enum import Enum
import math
import numpy as np
import datetime
#from celery.result import AsyncResult
from celery import signature
from celery import group, chain, chord, signature
import sqlalchemy as s
from sqlalchemy import or_, and_, update
from augur.application.logs import AugurLogger
from augur.tasks.init.celery_app import celery_app as celery
from augur.application.db.models import CollectionStatus, Repo
from augur.application.db.util import execute_session_query
from augur.application.config import AugurConfig
from augur.tasks.github.util.util import get_owner_repo, get_repo_weight_core, get_repo_weight_by_issue
from augur.tasks.github.util.gh_graphql_entities import GitHubRepo as GitHubRepoGraphql
from augur.tasks.github.util.gh_graphql_entities import GraphQlPageCollection
from augur.tasks.github.util.github_task_session import GithubTaskManifest
from augur.application.db.session import DatabaseSession
from augur.tasks.util.worker_util import calculate_date_weight_from_timestamps


# class syntax
class CollectionState(Enum):
    SUCCESS = "Success"
    PENDING = "Pending"
    ERROR = "Error"
    COLLECTING = "Collecting"
    INITIALIZING = "Initializing"
    UPDATE = "Update"
    FAILED_CLONE = "Failed Clone"

def get_list_of_all_users(session):
    #Get a list of all users.
    query = s.sql.text("""
        SELECT  
        user_id
        FROM augur_operations.users
    """)

    users = session.execute_sql(query).fetchall()
    return users


def get_required_conditions_for_core_repos(allow_collected_before = False, days_until_collect_again = 1):

    if not allow_collected_before:
        condition_concat_string = f"""
            core_status='{str(CollectionState.PENDING.value)}' AND core_status!='{str(CollectionState.ERROR.value)}'
            AND augur_operations.collection_status.core_data_last_collected IS NULL
            AND core_status!='{str(CollectionState.COLLECTING.value)}'
        """
    else:
        condition_concat_string = f"""
            core_status='Success' AND core_status!='{str(CollectionState.ERROR.value)}'
            AND augur_operations.collection_status.core_data_last_collected IS NOT NULL
            AND core_status!='{str(CollectionState.COLLECTING.value)}'
            AND core_data_last_collected <= NOW() - INTERVAL '{days_until_collect_again} DAYS'
        """
    
    return condition_concat_string

def get_required_conditions_for_secondary_repos(allow_collected_before = False, days_until_collect_again = 1):

    if not allow_collected_before:
        condition_concat_string = f"""
            secondary_status='{str(CollectionState.PENDING.value)}' AND secondary_status!='{str(CollectionState.ERROR.value)}'
            AND augur_operations.collection_status.core_status = '{str(CollectionState.SUCCESS.value)}' 
            AND augur_operations.collection_status.secondary_data_last_collected IS NULL
            AND secondary_status!='{str(CollectionState.COLLECTING.value)}'
        """
    else:
        condition_concat_string = f"""
            secondary_status='Success' AND secondary_status!='{str(CollectionState.ERROR.value)}'
            AND augur_operations.collection_status.secondary_data_last_collected IS NOT NULL
            AND augur_operations.collection_status.core_status = '{str(CollectionState.SUCCESS.value)}'
            AND secondary_status!='{str(CollectionState.COLLECTING.value)}'
            AND secondary_data_last_collected <= NOW() - INTERVAL '{days_until_collect_again} DAYS'
        """
    
    return condition_concat_string

def get_required_conditions_for_facade_repos(allow_collected_before = False, days_until_collect_again = 1):

    if not allow_collected_before:
        condition_concat_string = f"""
            facade_status='{str(CollectionState.UPDATE.value)}' AND facade_status!='{str(CollectionState.ERROR.value)}'
            AND augur_operations.collection_status.facade_status != '{str(CollectionState.PENDING.value)}'
            AND augur_operations.collection_status.facade_status != '{str(CollectionState.FAILED_CLONE.value)}'
            AND augur_operations.collection_status.facade_status != '{str(CollectionState.INITIALIZING.value)}'
            AND augur_operations.collection_status.facade_data_last_collected IS NULL
            AND facade_status!='{str(CollectionState.COLLECTING.value)}'
        """
    else:
        condition_concat_string = f"""
            facade_status='Success' AND facade_status!='{str(CollectionState.ERROR.value)}'
            AND augur_operations.collection_status.facade_data_last_collected IS NOT NULL
            AND augur_operations.collection_status.facade_status != '{str(CollectionState.PENDING.value)}'
            AND augur_operations.collection_status.facade_status != '{str(CollectionState.FAILED_CLONE.value)}'
            AND augur_operations.collection_status.facade_status != '{str(CollectionState.INITIALIZING.value)}'
            AND facade_status!='{str(CollectionState.COLLECTING.value)}'
            AND facade_data_last_collected <= NOW() - INTERVAL '{days_until_collect_again} DAYS'
        """
    
    return condition_concat_string

def get_required_conditions_for_ml_repos(allow_collected_before = False, days_until_collect_again = 1):

    if not allow_collected_before:
        condition_concat_string = f"""
            ml_status='{str(CollectionState.PENDING.value)}' AND ml_status!='{str(CollectionState.ERROR.value)}'
            AND augur_operations.collection_status.secondary_status = '{str(CollectionState.SUCCESS.value)}'
            AND augur_operations.collection_status.ml_data_last_collected IS NULL
            AND ml_status!='{str(CollectionState.COLLECTING.value)}'
        """
    else:
        condition_concat_string = f"""
            ml_status='Success' AND ml_status!='{str(CollectionState.ERROR.value)}'
            AND augur_operations.collection_status.ml_data_last_collected IS NOT NULL
            AND ml_status!='{str(CollectionState.COLLECTING.value)}'
            AND ml_data_last_collected <= NOW() - INTERVAL '{days_until_collect_again} DAYS'
        """
    
    return condition_concat_string



class CollectionRequest:
    def __init__(self,name,phases,max_repo = 10,days_until_collect_again = 1):
        self.name = name
        self.phases = phases
        self.max_repo = max_repo
        self.days_until_collect_again = days_until_collect_again
        self.new_status = CollectionState.PENDING.value
        self.repo_list = []

        self.status_column = f"{name}_status"


        if name == "facade":
            self.new_status = CollectionState.UPDATE.value

    def get_active_repo_count(self,session):
        return len(session.query(CollectionStatus).filter(getattr(CollectionStatus,f"{self.name}_status" ) == CollectionState.COLLECTING.value).all())

    #Get repo urls based on passed in info.
    def get_valid_repos(self,session):
        #getattr(CollectionStatus,f"{hook}_status" ) represents the status of the given hook
        #Get the count of repos that are currently running this collection hook
        #status_column = f"{hook}_status"
        active_repo_count = self.get_active_repo_count(session)

        #Will always disallow errored repos and repos that are already collecting

        #The maximum amount of repos to schedule is affected by the existing repos running tasks
        limit = self.max_repo-active_repo_count

        #Extract the user id from the randomized list and split into four chunks
        split_user_list = split_random_users_list(session,f"{self.name}_status",self.new_status)

        session.logger.info(f"User_list: {split_user_list}")

        #Iterate through each fourth of the users fetched
        for quarter_list in split_user_list:
            if limit <= 0:
                return

            collection_list = get_valid_repos_for_users(session,limit,tuple(quarter_list),hook=self.name, days_to_wait_until_next_collection=self.days_until_collect_again)

            self.repo_list.extend(collection_list)
            #Update limit with amount of repos started
            limit -= len(collection_list)

        #Now start old repos if there is space to do so.
        if limit <= 0:
            return


        user_list = get_list_of_all_users(session)
        random.shuffle(user_list)

        #Extract the user id from the randomized list and split into four chunks
        split_user_list = split_list_into_chunks([row[0] for row in user_list], 4)

        for quarter_list in split_user_list:
        
            #Break out if limit has been reached
            if limit <= 0:
                return

            #only start repos older than the specified amount of days
            #Query a set of valid repositories sorted by weight, also making sure that the repos aren't new or errored
            #Order by the relevant weight for the collection hook
            collection_list = get_valid_repos_for_users(session,limit,tuple(quarter_list),allow_old_repos=True,hook=self.name, days_to_wait_until_next_collection=self.days_until_collect_again)

            self.repo_list.extend(collection_list)
            limit -= len(collection_list)


def get_enabled_phase_names_from_config(logger, session):

    config = AugurConfig(logger, session)
    phase_options = config.get_section("Task_Routine")

    #Get list of enabled phases 
    enabled_phase_names = [name for name, phase in phase_options.items() if phase == 1]

    return enabled_phase_names

#Query db for CollectionStatus records that fit the desired condition.
#Used to get CollectionStatus for differant collection hooks
def get_collection_status_repo_git_from_filter(session,filter_condition,limit,order=None):

    if order is not None:
        repo_status_list = session.query(CollectionStatus).order_by(order).filter(filter_condition).limit(limit).all()
    else:
        repo_status_list = session.query(CollectionStatus).filter(filter_condition).limit(limit).all()

    return [status.repo.repo_git for status in repo_status_list]


def split_list_into_chunks(given_list, num_chunks):
    #Split list up into four parts with python list comprehension
    #variable n is the 
    n = 1 + (len(given_list) // num_chunks)
    return [given_list[i:i + n] for i in range(0, len(given_list),n)]


@celery.task
def task_failed_util(request,exc,traceback):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(task_failed_util.__name__)

    # log traceback to error file
    logger.error(f"Task {request.id} raised exception: {exc}\n{traceback}")
    
    with DatabaseSession(logger,engine) as session:
        core_id_match = CollectionStatus.core_task_id == request.id
        secondary_id_match = CollectionStatus.secondary_task_id == request.id
        facade_id_match = CollectionStatus.facade_task_id == request.id

        query = session.query(CollectionStatus).filter(or_(core_id_match,secondary_id_match,facade_id_match))

        print(f"chain: {request.chain}")
        #Make sure any further execution of tasks dependent on this one stops.
        try:
            #Replace the tasks queued ahead of this one in a chain with None.
            request.chain = None
        except AttributeError:
            pass #Task is not part of a chain. Normal so don't log.
        except Exception as e:
            logger.error(f"Could not mutate request chain! \n Error: {e}")
        
        try:
            collectionRecord = execute_session_query(query,'one')
        except:
            #Exit if we can't find the record.
            return
        
        if collectionRecord.core_task_id == request.id:
            # set status to Error in db
            collectionRecord.core_status = CollectionStatus.ERROR.value
            collectionRecord.core_task_id = None
        

        if collectionRecord.secondary_task_id == request.id:
            # set status to Error in db
            collectionRecord.secondary_status = CollectionStatus.ERROR.value
            collectionRecord.secondary_task_id = None
            
        
        if collectionRecord.facade_task_id == request.id:
            #Failed clone is differant than an error in collection.
            if collectionRecord.facade_status != CollectionStatus.FAILED_CLONE.value or collectionRecord.facade_status != CollectionStatus.UPDATE.value:
                collectionRecord.facade_status = CollectionStatus.ERROR.value

            collectionRecord.facade_task_id = None
        
        session.commit()
    


#This task updates the core and secondary weight with the issues and prs already passed in
@celery.task
def issue_pr_task_update_weight_util(issue_and_pr_nums,repo_git=None,session=None):
    from augur.tasks.init.celery_app import engine
    logger = logging.getLogger(issue_pr_task_update_weight_util.__name__)

    if repo_git is None:
        return
    
    if session is not None:
        update_issue_pr_weights(logger, session, repo_git, sum(issue_and_pr_nums))
    else:
        with DatabaseSession(logger,engine=engine) as session:
            update_issue_pr_weights(logger,session,repo_git,sum(issue_and_pr_nums))


@celery.task
def core_task_success_util(repo_git):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(core_task_success_util.__name__)

    logger.info(f"Repo '{repo_git}' succeeded through core collection")

    with DatabaseSession(logger, engine) as session:

        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        collection_status = repo.collection_status[0]

        collection_status.core_status = CollectionState.SUCCESS.value
        collection_status.core_data_last_collected = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        collection_status.core_task_id = None

        session.commit()

        repo_git = repo.repo_git
        status = repo.collection_status[0]
        raw_count = status.issue_pr_sum

        #Update the values for core and secondary weight
        issue_pr_task_update_weight_util([int(raw_count)],repo_git=repo_git,session=session)

#Update the existing core and secondary weights as well as the raw sum of issues and prs
def update_issue_pr_weights(logger,session,repo_git,raw_sum):
    repo = Repo.get_by_repo_git(session, repo_git)
    status = repo.collection_status[0]

    try: 
        weight = raw_sum

        weight -= calculate_date_weight_from_timestamps(repo.repo_added, status.core_data_last_collected)

        secondary_tasks_weight = raw_sum - calculate_date_weight_from_timestamps(repo.repo_added, status.secondary_data_last_collected)

        ml_tasks_weight = raw_sum - calculate_date_weight_from_timestamps(repo.repo_added,status.ml_data_last_collected)
    except Exception as e:
        logger.error(f"{e}")
        weight = None
        secondary_tasks_weight = None

    logger.info(f"Repo {repo_git} has a weight of {weight}")

    logger.info(f"Args: {raw_sum} , {repo_git}")

    if weight is None:
        return


    update_query = (
        update(CollectionStatus)
        .where(CollectionStatus.repo_id == repo.repo_id)
        .values(core_weight=weight,issue_pr_sum=raw_sum,secondary_weight=secondary_tasks_weight,ml_weight=ml_tasks_weight)
    )

    session.execute(update_query)
    session.commit()



@celery.task
def secondary_task_success_util(repo_git):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(secondary_task_success_util.__name__)

    logger.info(f"Repo '{repo_git}' succeeded through secondary collection")

    with DatabaseSession(logger, engine) as session:

        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        collection_status = repo.collection_status[0]

        collection_status.secondary_status = CollectionState.SUCCESS.value
        collection_status.secondary_data_last_collected	 = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        collection_status.secondary_task_id = None

        session.commit()

        #Update the values for core and secondary weight
        repo_git = repo.repo_git
        status = repo.collection_status[0]
        raw_count = status.issue_pr_sum

        issue_pr_task_update_weight_util([int(raw_count)],repo_git=repo_git,session=session)

#Get the weight for each repo for the secondary collection hook.
def get_repo_weight_secondary(logger,repo_git):
    from augur.tasks.init.celery_app import engine

    with DatabaseSession(logger,engine) as session:
        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        status = repo.collection_status[0]

        last_collected = status.secondary_data_last_collected

        if last_collected:
            time_delta = datetime.datetime.now() - status.secondary_data_last_collected
            days = time_delta
        else:
            days = 0

        return get_repo_weight_by_issue(logger, repo_git, days)


@celery.task
def facade_task_success_util(repo_git):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(facade_task_success_util.__name__)

    logger.info(f"Repo '{repo_git}' succeeded through facade task collection")

    with DatabaseSession(logger, engine) as session:

        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        collection_status = repo.collection_status[0]

        collection_status.facade_status = CollectionState.SUCCESS.value
        collection_status.facade_data_last_collected = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        collection_status.facade_task_id = None

        session.commit()

@celery.task
def ml_task_success_util(repo_git):
    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(facade_task_success_util.__name__)

    logger.info(f"Repo '{repo_git}' succeeded through machine learning task collection")

    with DatabaseSession(logger, engine) as session:

        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        collection_status = repo.collection_status[0]

        collection_status.ml_status = CollectionState.SUCCESS.value
        collection_status.ml_data_last_collected = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        collection_status.ml_task_id = None

        session.commit()



@celery.task
def facade_clone_success_util(repo_git):

    from augur.tasks.init.celery_app import engine

    logger = logging.getLogger(facade_clone_success_util.__name__)

    logger.info(f"Repo '{repo_git}' succeeded through facade update/clone")

    with DatabaseSession(logger, engine) as session:

        repo = Repo.get_by_repo_git(session, repo_git)
        if not repo:
            raise Exception(f"Task with repo_git of {repo_git} but could not be found in Repo table")

        collection_status = repo.collection_status[0]

        collection_status.facade_status = CollectionState.UPDATE.value
        collection_status.facade_task_id = None

        session.commit()


class AugurCollectionTotalRepoWeight:
    """
        small class to encapsulate the weight calculation of each repo that is
        being scheduled. Intended to be used as a counter where while it is greater than
        one it is subtracted from until it reaches zero. The weight calculation starts
        from a default method for core repos and can be passed differant calculations accordingly
        as a function that takes a repo_git


    Attributes:
        logger (Logger): Get logger from AugurLogger
        value (int): current value of the collection weight
        value_weight_calculation (function): Function to use on repo to determine weight
    """
    def __init__(self,starting_value: int, weight_calculation=get_repo_weight_core):
        self.logger = AugurLogger("data_collection_jobs").get_logger()
        self.value = starting_value
        self.value_weight_calculation = weight_calculation
    
    #This class can have it's value subtracted using a Repo orm class
    #or a plain integer value.
    def __sub__(self, other):

        if isinstance(other, int):
            self.value -= other
        elif isinstance(other, AugurCollectionTotalRepoWeight):
            self.value -= other.value
        elif isinstance(other, Repo):
            repo_weight = self.value_weight_calculation(self.logger,other.repo_git)
            self.value -= repo_weight
        elif isinstance(other, str):
            repo_weight = self.value_weight_calculation(self.logger,other)
            self.value -= repo_weight
        else:
            raise TypeError(f"Could not subtract object of type {type(other)}")

        if self.value < 0:
            self.value = 0

        return self


class AugurTaskRoutine:
    """
        class to keep track of various groups of collection tasks for a group of repos.
        Simple version to just schedule a number of repos not worrying about repo weight.
        The repo weight matters when constructing the CollectionRequest through get_valid_repos
        Used when scheduling repo clones/updates.


    Attributes:
        logger (Logger): Get logger from AugurLogger
        repos (List[str]): List of repo_ids to run collection on.
        collection_phases (List[str]): List of phases to run in augur collection.
        collection_hook (str): String determining the attributes to update when collection for a repo starts. e.g. core
        session: Database session to use
    """
    def __init__(self,session,collection_hooks):
        self.logger = session.logger

        self.collection_hooks = collection_hooks
        self.session = session

    def update_status_and_id(self,repo_git, task_id, name):
        repo = self.session.query(Repo).filter(Repo.repo_git == repo_git).one()

        #Set status in database to collecting
        repoStatus = repo.collection_status[0]
        #
        setattr(repoStatus,f"{name}_task_id",task_id)
        setattr(repoStatus,f"{name}_status", CollectionState.COLLECTING.value)
        self.session.commit()


    def start_data_collection(self):
        """Start all task items and return.

            The purpose is to encapsulate both preparing each message to the broker
            and starting the tasks for each repo in a general sense.
            This way all the specific stuff for each collection hook/ repo
            is generalized.
        """

        #Send messages starts each repo and yields its running info
        #to concurrently update the correct field in the database.
        for repo_git, task_id, hook_name in self.send_messages():
            self.update_status_and_id(repo_git,task_id,hook_name)
    
    def send_messages(self):
        augur_collection_list = []
        
        for col_hook in self.collection_hooks:

            self.logger.info(f"Starting collection on {len(col_hook.repo_list)} {col_hook.name} repos")

            for repo_git in col_hook.repo_list:

                #repo = self.session.query(Repo).filter(Repo.repo_git == repo_git).one()
                #repo_id = repo.repo_id

                augur_collection_sequence = []
                for job in col_hook.phases:
                    #Add the phase to the sequence in order as a celery task.
                    #The preliminary task creates the larger task chain 
                    augur_collection_sequence.append(job(repo_git))

                #augur_collection_sequence.append(core_task_success_util.si(repo_git))
                #Link all phases in a chain and send to celery
                augur_collection_chain = chain(*augur_collection_sequence)
                task_id = augur_collection_chain.apply_async().task_id

                self.logger.info(f"Setting repo {col_hook.name} status to collecting for repo: {repo_git}")

                #yield the value of the task_id to the calling method so that the proper collectionStatus field can be updated
                yield repo_git, task_id, col_hook.name

#def start_block_of_repos(logger,session,repo_git_identifiers,phases,repos_type,hook="core"):
#
#    logger.info(f"Starting collection on {len(repo_git_identifiers)} {repos_type} {hook} repos")
#    if len(repo_git_identifiers) == 0:
#        return 0
#    
#    logger.info(f"Collection starting for {hook}: {tuple(repo_git_identifiers)}")
#
#    routine = AugurTaskRoutine(session,repos=repo_git_identifiers,collection_phases=phases,collection_hook=hook)
#
#    routine.start_data_collection()
#
#    return len(repo_git_identifiers)

def get_valid_repos_for_users(session,limit,users,allow_old_repos = False,hook="core",days_to_wait_until_next_collection = 1):

    condition_string = "1"

    if hook == "core":
        condition_string = get_required_conditions_for_core_repos(allow_collected_before=allow_old_repos,days_until_collect_again= days_to_wait_until_next_collection)
    elif hook == "secondary":
        condition_string = get_required_conditions_for_secondary_repos(allow_collected_before=allow_old_repos,days_until_collect_again = days_to_wait_until_next_collection)
    elif hook == "facade":
        condition_string = get_required_conditions_for_facade_repos(allow_collected_before=allow_old_repos,days_until_collect_again = days_to_wait_until_next_collection)
    elif hook == "ml":
        condition_string = get_required_conditions_for_ml_repos(allow_collected_before=allow_old_repos,days_until_collect_again = days_to_wait_until_next_collection)

    #Query a set of valid repositories sorted by weight, also making sure that the repos are new
    #Order by the relevant weight for the collection hook
    repo_query = s.sql.text(f"""
        SELECT DISTINCT repo.repo_id, repo.repo_git, collection_status.{hook}_weight
        FROM augur_operations.user_groups 
        JOIN augur_operations.user_repos ON augur_operations.user_groups.group_id = augur_operations.user_repos.group_id
        JOIN augur_data.repo ON augur_operations.user_repos.repo_id = augur_data.repo.repo_id
        JOIN augur_operations.collection_status ON augur_operations.user_repos.repo_id = augur_operations.collection_status.repo_id
        WHERE user_id IN :list_of_user_ids AND {condition_string}
        ORDER BY augur_operations.collection_status.{hook}_weight
        LIMIT :limit_num
    """).bindparams(list_of_user_ids=users,limit_num=limit)

    #Get a list of valid repo ids, limit set to 2 times the usual
    valid_repos = session.execute_sql(repo_query).fetchall()
    valid_repo_git_list = [repo[1] for repo in valid_repos]

    session.logger.info(f"valid repo git list: {tuple(valid_repo_git_list)}")
    
    #start repos for new primary collection hook
    #collection_size = start_block_of_repos(
    #    session.logger, session,
    #    valid_repo_git_list,
    #    phases, repos_type=repos_type, hook=hook
    #)

    return valid_repo_git_list

def split_random_users_list(session,status_col, status_new):
    #Split all users that have new repos into four lists and randomize order
    query = s.sql.text(f"""
        SELECT  
        user_id
        FROM augur_operations.user_groups 
        JOIN augur_operations.user_repos ON augur_operations.user_groups.group_id = augur_operations.user_repos.group_id
        JOIN augur_data.repo ON augur_operations.user_repos.repo_id = augur_data.repo.repo_id
        JOIN augur_operations.collection_status ON augur_operations.user_repos.repo_id = augur_operations.collection_status.repo_id
        WHERE {status_col}='{str(status_new)}'
        GROUP BY user_id
    """)

    user_list = session.execute_sql(query).fetchall()
    random.shuffle(user_list)

    #Extract the user id from the randomized list and split into four chunks
    split_user_list = split_list_into_chunks([row[0] for row in user_list], 4)

    return split_user_list

