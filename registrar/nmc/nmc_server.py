# -*- coding: utf-8 -*-
"""
    Registrar
    ~~~~~

    copyright: (c) 2014 by Halfmoon Labs, Inc.
    copyright: (c) 2015 by Blockstack.org

This file is part of Registrar.

    Registrar is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Registrar is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Registrar. If not, see <http://www.gnu.org/licenses/>.
"""

import os
import json
import datetime
import hashlib

from pymongo import MongoClient
from time import sleep

from pybitcoin.rpc import namecoind
from pybitcoin.rpc.namecoind_cluster import pending_transactions

from registrar.nameops import process_user
from registrar.transfer import transfer_name, nameTransferred

from tools.sweep_btc import sweep_btc
from tools.misc import import_user, import_update
from tools.bip38 import bip38_decrypt

from .config import MONGODB_URI, OLD_DB, AWSDB_URI
from .config_local import problem_users, banned_users

remote_client = MongoClient(MONGODB_URI)
remote_db = remote_client.get_default_database()
users = remote_db.user
registrations = remote_db.user_registration
updates = remote_db.profile_update
transfer = remote_db.name_transfer

old_client = MongoClient(OLD_DB)
old_db = old_client.get_default_database()
old_users = old_db.user

aws_db = MongoClient(AWSDB_URI)['blockdata']
register_queue = aws_db.queue

from registrar.config import LOAD_SERVERS
load_servers = LOAD_SERVERS

current_server = 0

MAX_PENDING_TX = 50


def load_balance():

    global current_server

    print "current server: %s" %load_servers[current_server]

    while(1):
        if pending_transactions(load_servers[current_server]) > MAX_PENDING_TX:

            if current_server == len(load_servers) - 1:
                current_server = 0
            else:
                current_server += 1

            print "load balancing: switching to %s" %load_servers[current_server]
            sleep(30)

        else:
            break


def process_profile(username, profile, new_address=None):

    if username in problem_users:
        return

    try:
        process_user(username, profile, load_servers[current_server], new_address)
    except Exception as e:
        print e


def profile_on_blockchain(username, DB_profile):

    try:
        block_profile = namecoind.get_full_profile('u/' + username)
    except:
        return False

    block_profile = json.dumps(block_profile, sort_keys=True)
    DB_profile = json.dumps(DB_profile, sort_keys=True)

    if len(block_profile) == len(DB_profile):
        #check hash for only profiles where length is the same
        if hashlib.md5(block_profile).hexdigest() == hashlib.md5(DB_profile).hexdigest():
            return True
        else:
            return False
    else:
        return False


def check_banned(username):

    for user in banned_users:
        if user in username:
            return True

    return False


def register_users():

    counter = 0

    for new_user in registrations.find():

        user_id = new_user['user_id']
        user = users.find_one({"_id": user_id})

        if user is None:
            continue

        if not user['username_activated']:
            continue

        if check_banned(user['username']):
            continue

        print "checking: " + user['username']

        counter += 1

        check_queue = register_queue.find_one({"key": 'u/' + user['username']})

        if check_queue is not None:
            print "Already in queue"
            continue

        if 'dispatched' in new_user and new_user['dispatched'] is False:

            print "Dispatch: " + user['username']

            process_profile(user['username'], user['profile'], new_address=user['namecoin_address'])
            new_user['dispatched'] = True
            registrations.save(new_user)

        elif 'dispatched' in new_user and new_user['dispatched'] is True:

            if profile_on_blockchain(user["username"], user["profile"]):

                if nameTransferred(user, namecoind):
                    print "cleaning: " + user["username"]
                    registrations.remove(new_user)
                else:
                    transfer_name(user['username'], user['namecoin_address'], live=True)
            else:
                process_profile(user['username'], user['profile'], new_address=user['namecoin_address'])

        if counter % 5 == 0:
            load_balance()

    print counter


def check_transfer():

    for new_user in transfer.find():

        user_id = new_user['user_id']
        user = users.find_one({"_id": user_id})

        if user is None:
            old_user = old_users.find_one({"_id": user_id})
            user = users.find_one({"username": old_user['username']})

        if check_banned(user['username']):
            continue

        if profile_on_blockchain(user["username"], user["profile"]):
            pass
        else:
            print "Problem: " + user["username"]
            import_user(user["username"])
            process_profile(user['username'], user['profile'])


def update_users():

    for new_user in updates.find():

        user_id = new_user['user_id']
        user = users.find_one({"_id": user_id})

        if user is None:
            old_user = old_users.find_one({"_id": user_id})
            user = users.find_one({"username": old_user['username']})

        if check_banned(user['username']):
            continue

        if profile_on_blockchain(user["username"], user["profile"]):
            updates.remove(new_user)
        else:
            print "Update: " + str(user['username'])
            import_update(new_user)
            process_profile(user['username'], user['profile'], new_address=user['namecoin_address'])


def cleanup_db():

    print "----------"
    print "Cleaning DB"

    for new_user in updates.find():

        user_id = new_user['user_id']
        user = users.find_one({"_id": user_id})

        if user is None:
            old_user = old_users.find_one({"_id": user_id})
            user = users.find_one({"username": old_user['username']})

        if check_banned(user['username']):
            continue

        try:
            if profile_on_blockchain(user["username"], user["profile"]):
                print "cleaning: " + user["username"]
                updates.remove(new_user)
        except:
            pass

    for new_user in transfer.find():

        user_id = new_user['user_id']
        user = users.find_one({"_id": user_id})

        if user is None:
            old_user = old_users.find_one({"_id": user_id})
            user = users.find_one({"username": old_user['username']})       

        if check_banned(user['username']):
            continue

        if sweep_btc(new_user, LIVE=False):
            print "Sweep BTC"
        else:
            if profile_on_blockchain(user["username"], user["profile"]):
                print "cleaning: " + user["username"]
                transfer.remove(new_user)

    print "----------"


def get_pending_state():

    print "Pending registrations: %s" % registrations.count()
    print "Pending updates: %s" % updates.count()
    print "Pending transfers: %s" % transfer.count()


if __name__ == '__main__':

    #cleanup_db()
    #update_users()
    register_users()
