"""Script does several things:
1. checks if the first posts of the searches were changed
2. checks active searches' status (Ищем, НЖ, НП, etc.)
3. checks active searches' visibility (accessible for everyone, restricted to a certain group or permanently deleted).
Updates are either saved in PSQL or send via pub/sub to other scripts"""

import os
import requests
import datetime
import re
import json
import logging
import difflib
import hashlib
import random

import sqlalchemy
# idea for optimization – to move to psycopg2

from google.cloud import secretmanager
from google.cloud import pubsub_v1


project_id = os.environ["GCP_PROJECT"]
client = secretmanager.SecretManagerServiceClient()
requests_session = requests.Session()
publisher = pubsub_v1.PublisherClient()

bad_gateway_counter = 0


def get_secrets(secret_request):
    """get GCP secret"""

    name = f"projects/{project_id}/secrets/{secret_request}/versions/latest"
    response = client.access_secret_version(name=name)

    return response.payload.data.decode("UTF-8")


def sql_connect():
    """connect to PSQL in GCP"""

    db_user = get_secrets("cloud-postgres-username")
    db_pass = get_secrets("cloud-postgres-password")
    db_name = get_secrets("cloud-postgres-db-name")
    db_conn = get_secrets("cloud-postgres-connection-name")
    db_socket_dir = "/cloudsql"

    db_config = {
        "pool_size": 5,
        "max_overflow": 0,
        "pool_timeout": 0,  # seconds
        "pool_recycle": 120,  # seconds
    }

    pool = sqlalchemy.create_engine(
        sqlalchemy.engine.url.URL(
            "postgresql+pg8000",
            username=db_user,
            password=db_pass,
            database=db_name,
            query={
                "unix_sock": "{}/{}/.s.PGSQL.5432".format(
                    db_socket_dir,
                    db_conn)
            }
        ),
        **db_config
    )
    pool.dialect.description_encoding = None

    return pool


def publish_to_pubsub(topic_name, message):
    """publish a new message to pub/sub"""

    topic_path = publisher.topic_path(project_id, topic_name)
    message_json = json.dumps({'data': {'message': message}, })
    message_bytes = message_json.encode('utf-8')

    try:
        publish_future = publisher.publish(topic_path, data=message_bytes)
        publish_future.result()  # Verify the publishing succeeded
        logging.info(f'Sent pub/sub message: {message}')

    except Exception as e:
        logging.info(f'Not able to send pub/sub message: {message}')
        logging.exception(e)

    return None


def notify_admin(message):
    """send the pub/sub message to Debug to Admin"""

    publish_to_pubsub('topic_notify_admin', message)

    return None


def check_topic_visibility(search_num):
    """check is the existing search was deleted or hidden"""

    global bad_gateway_counter

    deleted_trigger = None
    hidden_trigger = None
    visibility = 'regular'
    bad_gateway = False

    content = parse_search(search_num)

    if content:

        # TODO: temp check, if this "patch" can help to remove timeouts
        bad_gateway = True if content.find('') > 0 else False

        # FIXME – below is a check if content.find('') > 0 is not always True
        if content.find('') > 0:
            notify_admin(f'content.find() > 0 is True for {search_num}. BadGateway = {bad_gateway}')
        # FIXME – end

        # FIXME – below is a check if content.find('502 Bad Gateway') is a right format for bad_gateway
        test_old__bad_gateway = True if content.find('Bad Gateway') > 0 else False
        if test_old__bad_gateway:
            notify_admin(f'BadGateway for {search_num}, content[3000]:{content[3000]}')
        # FIXME – end

        if not bad_gateway:

            if content.find('Запрошенной темы не существует.') > -1:
                deleted_trigger = True
                visibility = 'deleted'

            else:
                deleted_trigger = False

            if content.find('Для просмотра этого форума вы должны быть авторизованы') > -1:
                hidden_trigger = True
                visibility = 'hidden'
            else:
                hidden_trigger = False

            logging.info(f'search {search_num} is {visibility}')

    return deleted_trigger, hidden_trigger, bad_gateway, visibility


def update_one_topic_visibility(search_id):
    """update the status of one search: if it is ok or was deleted or hidden"""

    global bad_gateway_counter
    global requests_session

    del_trig, hid_trig, bad_gateway_trigger, visibility = check_topic_visibility(search_id)

    logging.info(f'{search_id}: visibility = {visibility}')

    if not bad_gateway_trigger:

        pool = sql_connect()
        with pool.connect() as conn:

            try:
                stmt = sqlalchemy.text("""DELETE FROM search_health_check WHERE search_forum_num=:a;""")
                conn.execute(stmt, a=search_id)

                stmt = sqlalchemy.text("""INSERT INTO search_health_check (search_forum_num, timestamp, status) 
                                    VALUES (:a, :b, :c);""")
                conn.execute(stmt, a=search_id, b=datetime.datetime.now(), c=visibility)

                logging.info('psql updated for {} status is set {}'.format(search_id, visibility))
                logging.info('---------------')

            except Exception as e:
                logging.info('exception in update_one_topic_visibility')
                logging.exception(e)

            conn.close()
        pool.dispose()

    else:
        bad_gateway_counter += 1
        logging.info('502: {} - {}'.format(str(search_id), bad_gateway_counter))

    return None


def update_visibility_for_list_of_active_searches(number_of_searches):
    """update the status of all active searches if it was deleted of hidden"""

    global bad_gateway_counter
    global requests_session

    pool = sql_connect()
    conn = pool.connect()

    try:
        full_list_of_active_searches = conn.execute("""
            SELECT 
                s3.* 
            FROM (
                SELECT
                    s1.status_short, s1.search_forum_num, s1.forum_search_title, s2.status, s2.timestamp, 
                    s1.forum_folder_id 
                FROM
                    searches s1 
                LEFT JOIN 
                    search_health_check s2 
                ON
                    s1.search_forum_num = s2.search_forum_num 
                WHERE 
                    s1.status_short = 'Ищем' 
                    AND s2.status != 'deleted'
            ) s3 
            LEFT JOIN 
                folders f 
            ON 
                s3.forum_folder_id=f.folder_id 
            WHERE 
                f.folder_type IS NULL 
                OR f.folder_type = 'searches' 
            ORDER BY s3.timestamp 
            /*action='get_full_list_of_active_searches 2.0' */
            ;
            """).fetchall()

        cleared_list_of_active_searches = []

        # first we add new lines to the list
        for line in full_list_of_active_searches:
            search = list(line)
            if not search[3]:
                cleared_list_of_active_searches.append(search)

        # then we add not-new lines that are not deleted
        for line in full_list_of_active_searches:
            search = list(line)
            if search[3] and search[3] != 'deleted':
                cleared_list_of_active_searches.append(search)
                if len(cleared_list_of_active_searches) >= number_of_searches:
                    break

        if cleared_list_of_active_searches:
            logging.info(f'length of cleared list of active searches is {len(cleared_list_of_active_searches)}')
            logging.info(f'cleared list of active searches: {cleared_list_of_active_searches}')

            for search in cleared_list_of_active_searches:

                update_one_topic_visibility(search[1])

                if bad_gateway_counter > 3:
                    break

    except Exception as e:
        logging.info('exception in get_and_update_list_of_active_searches')
        logging.exception(e)

    conn.close()
    pool.dispose()

    return None


def parse_search(search_num):
    """parse the whole search page"""

    global requests_session
    global bad_gateway_counter
    content = None

    try:
        url = f'https://lizaalert.org/forum/viewtopic.php?t={search_num}'
        r = requests_session.get(url, timeout=10)  # seconds – not sure if it is efficient in this case
        content = r.content.decode("utf-8")

    except requests.exceptions.ReadTimeout:
        logging.info(f'[che_posts]: requests.exceptions.ReadTimeout')
        notify_admin(f'[che_posts]: requests.exceptions.ReadTimeout')
        bad_gateway_counter += 1

    except requests.exceptions.Timeout:
        logging.info(f'[che_posts]: requests.exceptions.Timeout')
        notify_admin(f'[che_posts]: requests.exceptions.Timeout')
        bad_gateway_counter += 1

    except ConnectionError:
        logging.info(f'[che_posts]: CONNECTION ERROR OR TIMEOUT')
        notify_admin(f'[che_posts]: CONNECTION ERROR OR TIMEOUT')
        bad_gateway_counter += 1

    except Exception as e:
        logging.info('[che_posts]: Unknown exception')
        logging.exception(e)

    return content


def parse_first_post(search_num):
    """parse the first post of search"""

    hash_num = None
    bad_gateway = False
    not_found = False

    content = parse_search(search_num)

    if content:

        bad_gateway = True if content.find('502 Bad Gateway') > 0 else False
        not_found = True if content.find('Запрошенной темы не существует') > 0 else False

        if not bad_gateway and not not_found:

            get_status_from_content_and_send_to_topic_management(search_num, content)

            # cut the wording of the first post
            start = content.find('<div class="content">')
            content = content[(start + 21):]

            # find the next block and limit the content till this block
            next_block = content.find('<div class="back2top">')
            content = content[:(next_block - 12)]

            # cut out div closure
            fin_div = content.rfind('</div>')
            content = content[:fin_div]

            # cut blank symbols in the end of code
            finish = content.rfind('>')
            content = content[:(finish + 1)]

            # exclude dynamic info – views of the pictures
            patterns = re.findall(r'\) \d+ просмотр(?:а|ов)?', content)
            if patterns:
                for word in patterns:
                    content = content.replace(word, ")")

            # exclude dynamic info - token / creation time / sid / etc / footer
            patterns_list = [r'value="\S{10}"',
                             r'value="\S{32}"',
                             r'value="\S{40}"',
                             r'sid=\S{32}&amp;',
                             r'<span class="footer-info"><span title="SQL time:.{120,130}</span></span>'
                             ]

            patterns = []
            for pat in patterns_list:
                patterns += re.findall(pat, content)

            if patterns:
                for word in patterns:
                    content = content.replace(word, '')

            # craft a hash for this content
            hash_num = hashlib.md5(content.encode()).hexdigest()

    return hash_num, content, bad_gateway, not_found


def get_list_of_searches_for_first_post_and_status_update(percent_of_searches, weights):
    """get best list of searches for which first posts should be checked"""

    outcome_list = []
    base_table = []

    # there are five types of search parameters:
    # 1. search_start_time
    # 2. search_update_time
    # 3. folder weight
    # 4. number of checks already made
    # 5. random
    # we'll pick a certain amount of searches from overall list for each category

    if percent_of_searches > 0:

        pool = sql_connect()
        conn = pool.connect()

        try:
            # get the data from sql with the structure:
            # [search_id, search_start_time, forum_folder_id, search_update_time, number_of_searches_in_folder]
            # search_update_time – is a time of search's first post actualization in SQL
            # number_of_searches_in_folder – is a historical number of searches in SQL assigned to each folder
            raw_sql_extract = conn.execute("""
            SELECT 
                s4.*, s5.count 
            FROM (
                SELECT 
                    s2.*, s3.timestamp, s3.num_of_checks
                FROM (
                    SELECT 
                        s0.search_forum_num, s0.search_start_time, s0.forum_folder_id 
                    FROM (
                        SELECT 
                            search_forum_num, search_start_time, forum_folder_id
                        FROM
                            searches 
                        WHERE
                            status_short = 'Ищем'
                    ) s0 
                    LEFT JOIN 
                        search_health_check s1 
                    ON 
                        s0.search_forum_num=s1.search_forum_num 
                    WHERE
                        (s1.status != 'deleted' AND s1.status != 'hidden')
                ) s2 
                LEFT JOIN 
                (
                SELECT 
                    search_id, timestamp, actual, content_hash, num_of_checks 
                FROM
                    search_first_posts 
                WHERE
                    actual=TRUE
                ) s3 
                ON
                    s2.search_forum_num=s3.search_id
            ) s4 
            LEFT JOIN 
            (
                SELECT
                    count(*), forum_folder_id
                FROM
                    searches
                GROUP BY
                    forum_folder_id
                ORDER BY
                    count(*) DESC
            ) s5 
            ON 
                s4.forum_folder_id=s5.forum_folder_id
            LEFT JOIN
                folders AS f
            ON
                s4.forum_folder_id = f.folder_id
            WHERE 
                f.folder_type IS NULL OR f.folder_type = 'searches'
            /*action='get_list_of_searches_for_first_post_and_status_update 2.0' */        
            ;
            """).fetchall()

            # form the list-like table
            if raw_sql_extract:
                for line in raw_sql_extract:
                    new_line = [line[0], line[1], line[3], line[5], line[4]]
                    # for blank lines we paste the oldest date
                    if not line[3]:
                        new_line[2] = datetime.datetime(1, 1, 1, 0, 0)
                    # for blank lines we paste the oldest date
                    if not line[4]:
                        new_line[4] = 1
                    base_table.append(new_line)

            num_of_searches = round(len(base_table) * percent_of_searches / 100)

            # 1. sort the table by 1st arg = search_start_time
            # number 1 – should be the first to check,
            # number ∞ – should be the last to check
            base_table.sort(key=lambda x: x[1], reverse=True)
            i = 1
            for line in base_table:
                line.append(i)
                i += 1

            group_of_searches = round(weights["start_time"]/100*num_of_searches)

            for j in range(group_of_searches):
                outcome_list.append(base_table[j])

            # 2. sort the table by 2nd arg = search_update_time
            # number 1 – should be the first to check
            # number ∞ – should be the last to check
            base_table.sort(key=lambda x: x[2])
            i = 1
            for line in base_table:
                line.append(i)
                i += 1

            group_of_searches = round(weights["upd_time"] / 100 * num_of_searches)

            for j in range(len(base_table)):
                if base_table[j][0] not in [line[0] for line in outcome_list] and group_of_searches > 0:
                    outcome_list.append(base_table[j])
                    group_of_searches -= 1
                elif group_of_searches == 0:
                    break

            # 3. sort the table by 3rd arg = folder weight
            # number 1 – should be the first to check
            # number ∞ – should be the last to check
            base_table.sort(key=lambda x: x[3], reverse=True)
            i = 1
            for line in base_table:
                line.append(i)
                i += 1

            group_of_searches = round(weights["folder_weight"] / 100 * num_of_searches)

            for j in range(len(base_table)):
                if base_table[j][0] not in [line[0] for line in outcome_list] and group_of_searches > 0:
                    outcome_list.append(base_table[j])
                    group_of_searches -= 1
                elif group_of_searches == 0:
                    break

            # 4. sort the table by 4th arg = number of check that were already done
            # number 1 – should be the first to check
            # number ∞ – should be the last to check
            base_table.sort(key=lambda x: x[4])
            i = 1
            for line in base_table:
                line.append(i)
                i += 1

            group_of_searches = round(weights["checks_made"] / 100 * num_of_searches)

            for j in range(len(base_table)):
                if base_table[j][0] not in [line[0] for line in outcome_list] and group_of_searches > 0:
                    outcome_list.append(base_table[j])
                    group_of_searches -= 1
                elif group_of_searches == 0:
                    break

            # 5. get random searches for checks

            random.shuffle(base_table)

            group_of_searches = round(weights["random"] / 100 * num_of_searches)

            for j in range(len(base_table)):
                if base_table[j][0] not in [line[0] for line in outcome_list] and group_of_searches > 0:
                    outcome_list.append(base_table[j])
                    group_of_searches -= 1
                elif group_of_searches == 0:
                    break

        except Exception as e:
            logging.info('exception in get_list_of_searches_for_first_post_update')
            logging.exception(e)

        conn.close()
        pool.dispose()

    return outcome_list


def get_the_diff_between_strings(string_1, string_2):
    """get the text-message with the difference of two strings"""

    comparison = list(difflib.Differ().compare(string_1.splitlines(), string_2.splitlines()))

    output_message = ''

    for line in comparison:
        if line[0] in {'+', '-'}:
            output_message += line + '\n'

    return output_message


def get_status_from_content_and_send_to_topic_management(topic_id, act_content):
    """block to check if Status of the search has changed – if so send a pub/sub to topic_management"""

    # get the Title out of page content (intentionally avoid BS4 to make pack slimmer)
    pre_title = re.search(r'<h2 class="topic-title"><a href=.{1,500}</a>', act_content)
    pre_title = pre_title.group() if pre_title else None
    pre_title = re.search(r'">.{1,500}</a>', pre_title[32:]) if pre_title else None
    title = pre_title.group()[2:-4] if pre_title else None
    status = None
    if title:
        missed = re.search(r'(?i).{0,10}пропал.*', title) if title else None
        if missed:
            status = 'Ищем'
        else:
            missed = re.search(r'(?i).{0,10}(?:найден|).{0,5}жив', title)
            if missed:
                status = 'НЖ'
            else:
                missed = re.search(r'(?i).{0,10}(?:найден|).{0,5}пог', title)
                if missed:
                    status = 'НП'
                else:
                    missed = re.search(r'(?i).{0,10}заверш.н', title)
                    if missed:
                        status = 'Завершен'

    if status in {'НЖ', 'НП', 'Завершен'}:
        publish_to_pubsub('topic_for_topic_management', {'topic_id': topic_id, 'status': status})
        logging.info(f'pub/sub message for topic_management triggered: topic_id: {topic_id}, status: {status}')

    return None


def update_first_posts_and_statuses(percent_of_searches, weights):
    """periodically check if the first post of searches"""

    global bad_gateway_counter
    global requests_session

    list_of_searches_with_updated_first_posts = []
    list_of_searches = get_list_of_searches_for_first_post_and_status_update(percent_of_searches, weights)

    if list_of_searches:

        pool = sql_connect()
        conn = pool.connect()

        try:
            for line in list_of_searches:

                search_id = line[0]
                act_hash, act_content, bad_gateway_trigger, not_found_trigger = parse_first_post(search_id)

                if not bad_gateway_trigger and not not_found_trigger:

                    # check the latest hash
                    stmt = sqlalchemy.text("""
                    SELECT content_hash, num_of_checks, content from search_first_posts WHERE search_id=:a 
                    AND actual = TRUE;
                    """)
                    raw_data = conn.execute(stmt, a=search_id).fetchone()

                    # if record for this search – exists
                    if raw_data:

                        last_hash = raw_data[0]
                        prev_number_of_checks = raw_data[1]
                        # last_content = raw_data[2]

                        if not prev_number_of_checks:
                            prev_number_of_checks = 1

                        # if record for this search – outdated
                        if act_hash != last_hash:

                            # set all prev records as Actual = False
                            stmt = sqlalchemy.text("""
                            UPDATE search_first_posts SET actual = FALSE WHERE search_id = :a;
                            """)
                            conn.execute(stmt, a=search_id)

                            # add new record
                            stmt = sqlalchemy.text("""
                            INSERT INTO search_first_posts 
                            (search_id, timestamp, actual, content_hash, content, num_of_checks) 
                            VALUES (:a, :b, TRUE, :c, :d, :e);
                            """)
                            conn.execute(stmt, a=search_id, b=datetime.datetime.now(), c=act_hash,
                                         d=act_content, e=1)

                            # add the search into the list of searches to be sent to pub/sub
                            list_of_searches_with_updated_first_posts.append(search_id)

                        # if record for this search – actual
                        else:

                            # update the number of checks for this search
                            stmt = sqlalchemy.text("""
                                                UPDATE 
                                                    search_first_posts 
                                                SET 
                                                    num_of_checks = :a 
                                                WHERE 
                                                    search_id = :b AND actual = True;
                                                """)
                            conn.execute(stmt, a=(prev_number_of_checks + 1), b=search_id)

                    # if record for this search – does not exist – add a new record
                    else:

                        stmt = sqlalchemy.text("""
                                                INSERT INTO search_first_posts 
                                                (search_id, timestamp, actual, content_hash, content, num_of_checks) 
                                                VALUES (:a, :b, TRUE, :c, :d, :e);
                                                """)
                        conn.execute(stmt, a=search_id, b=datetime.datetime.now(), c=act_hash, d=act_content, e=1)

                elif bad_gateway_trigger:
                    bad_gateway_counter += 1
                    logging.info('502: {} - {}'.format(search_id, bad_gateway_counter))

                elif not_found_trigger:
                    update_one_topic_visibility(search_id)

        except Exception as e:
            logging.info('exception in update_first_posts_and_statuses')
            logging.exception(e)

        conn.close()
        pool.dispose()

    if list_of_searches_with_updated_first_posts:
        # send pub/sub message on the updated first page
        publish_to_pubsub('topic_for_first_post_processing', list_of_searches_with_updated_first_posts)

    return None


def main(event, context): # noqa
    """main function"""

    global bad_gateway_counter
    bad_gateway_counter = 0

    # BLOCK 1. for checking visibility (deleted or hidden) and status (Ищем, НЖ, НП) changes of active searches
    # A reason why this functionality – is in this script, is that it worth update the list of active searches first
    # and then check for first posts. Plus, once first posts checker finds something odd – it triggers a visibility
    # check for this search
    number_of_checked_searches = 100
    update_visibility_for_list_of_active_searches(number_of_checked_searches)

    # BLOCK 2. for checking if the first posts were changed
    # check is made for a certain % from the full list of active searches
    # below percent – is a matter of experiments: avoiding script timeout and minimize costs, but to get updates ASAP
    percent_of_first_posts_to_check = 20
    # the chosen number of searches, for which first posts will be checked:
    # [first_posts] = [all_act_searches] * [percent]
    # then the [first_posts] is split by 5 subcategories, which has different % (sum of % should be 100)
    # so the check for first posts updates is happening
    # 1. start_time – will help to check only the latest searches with "freshest" starting time
    # 2. upd_time – will help to check only searches with the oldest previous update time
    # 3. folder_weight – will help to check only searches in the most popular regions
    # 4. checks_made – will help to check only searches with fewer previous checks
    # 5. random – turned out a good solution to check other searches that don't fall into prev categories
    weights = {"start_time": 20, "upd_time": 20, "folder_weight": 20, "checks_made": 20, "random": 20}
    update_first_posts_and_statuses(percent_of_first_posts_to_check, weights)

    if bad_gateway_counter > 3:
        publish_to_pubsub('topic_notify_admin', f'[che_posts]: Bad Gateway {bad_gateway_counter} times')

    # Close the open session
    requests_session.close()

    return None
