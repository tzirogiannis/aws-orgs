#!/usr/bin/env python


"""Manage accounts in an AWS Organization.

Usage:
  awsaccounts (report|create|alias|invite) [--config FILE]
                                           [--spec-dir PATH] 
                                           [--master-account-id ID]
                                           [--auth-account-id ID]
                                           [--org-access-role ROLE]
                                           [--invited-account-id ID]
                                           [--exec] [-q] [-d|-dd]
  awsaccounts (--help|--version)

Modes of operation:
  report         Display organization status report.
  create         Create new accounts in AWS Org per specifation.
  alias          Set account alias for each account in Org per specifation.
  invite         Invite another account to join Org as a member account. 

Options:
  -h, --help                Show this help message and exit.
  -V, --version             Display version info and exit.
  -f, --config FILE         AWS Org config file in yaml format.
  --spec-dir PATH           Location of AWS Org specification file directory.
  --master-account-id ID    AWS account Id of the Org master account.    
  --auth-account-id ID      AWS account Id of the authentication account.
  --org-access-role ROLE    IAM role for traversing accounts in the Org.
  --invited-account-id ID   Id of account being invited to join Org.
                            Required when running in 'invite' mode.
  --exec                    Execute proposed changes to AWS accounts.
  --role ROLENAME           IAM role to use to access accounts.
  -q, --quiet               Repress log output.
  -d, --debug               Increase log level to 'DEBUG'.
  -dd                       Include botocore and boto3 logs in log stream.

"""


import yaml
import time

import boto3
import botocore
from botocore.exceptions import ClientError
from docopt import docopt

import awsorgs
from awsorgs.utils import *
from awsorgs.spec import *


def create_accounts(org_client, args, log, deployed_accounts, account_spec):
    """
    Compare deployed_accounts to list of accounts in the accounts spec.
    Create accounts not found in deployed_accounts.
    """
    for a_spec in account_spec['accounts']:
        if not lookup(deployed_accounts, 'Name', a_spec['Name']):
            # check if it is still being provisioned
            created_accounts = scan_created_accounts(log, org_client)
            if lookup(created_accounts, 'AccountName', a_spec['Name']):
                log.warn("New account '%s' is not yet available" % a_spec['Name'])
                break
            # create a new account
            if 'Email' in a_spec and a_spec['Email']:
                email_addr = a_spec['Email']
            else:
                email_addr = '%s@%s' % (a_spec['Name'], account_spec['default_domain'])
            log.info("Creating account '%s'" % (a_spec['Name']))
            log.debug('account email: %s' % email_addr)
            if args['--exec']:
                new_account = org_client.create_account(
                        AccountName=a_spec['Name'],
                        Email=email_addr)
                create_id = new_account['CreateAccountStatus']['Id']
                log.info("CreateAccountStatus Id: %s" % (create_id))
                # validate creation status
                counter = 0
                maxtries = 5
                while counter < maxtries:
                    creation = org_client.describe_create_account_status(
                            CreateAccountRequestId=create_id
                            )['CreateAccountStatus']
                    if creation['State'] == 'IN_PROGRESS':
                        time.sleep(5)
                        log.info("Account creation in progress for '%s'" %
                                a_spec['Name'])
                    elif creation['State'] == 'SUCCEEDED':
                        log.info("Account creation succeeded")
                        break
                    elif creation['State'] == 'FAILED':
                        log.error("Account creation failed: %s" %
                                creation['FailureReason'])
                        break
                    counter += 1
                if counter == maxtries and creation['State'] == 'IN_PROGRESS':
                     log.warn("Account creation still pending. Moving on!")


def set_account_alias(account, log, args, account_spec, role):
    """
    Set an alias on an account.  Use 'Alias' attribute from account spec
    if provided.  Otherwise set the alias to the account name.
    """
    if account['Status'] == 'ACTIVE':
        a_spec = lookup(account_spec['accounts'], 'Name', account['Name'])
        if a_spec and 'Alias' in a_spec:
            proposed_alias = a_spec['Alias']
        else:
            proposed_alias = account['Name'].lower()
        credentials = get_assume_role_credentials(
                account['Id'], args['--org-access-role'])
        if isinstance(credentials, RuntimeError):
            log.error(credentials)
        else:
            iam_client = boto3.client('iam', **credentials)
        aliases = iam_client.list_account_aliases()['AccountAliases']
        log.debug('account_name: %s; aliases: %s' % (account['Name'], aliases))
        if not aliases:
            log.info("setting account alias to '%s' for account '%s'" %
                    (proposed_alias, account['Name']))
            if args['--exec']:
                try:
                    iam_client.create_account_alias(AccountAlias=proposed_alias)
                except Exception as e:
                    log.error(e)
        elif aliases[0] != proposed_alias:
            log.info("resetting account alias for account '%s' to '%s'; "
                    "previous alias was '%s'" %
                    (account['Name'], proposed_alias, aliases[0]))
            if args['--exec']:
                iam_client.delete_account_alias(AccountAlias=aliases[0])
                try:
                    iam_client.create_account_alias(AccountAlias=proposed_alias)
                except Exception as e:
                    log.error(e)


def scan_invited_accounts(log, org_client):
    """Return a list of handshake IDs"""
    response = org_client.list_handshakes_for_organization(
            Filter={'ActionType': 'INVITE'})
    handshakes = response['Handshakes']
    while 'NextToken' in response:
        response = org_client.list_handshakes_for_organization(
                Filter={'ActionType': 'INVITE'},
                NextToken=response['NextToken'])
        handshakes += response['Handshakes']
    log.debug(handshakes)
    return handshakes


def invite_account(log, args, org_client, deployed_accounts):
    """Invite account_id to join Org"""
    account_id = args['--invited-account-id']
    if not account_id:
        log.critical("option '--invited-account-id' not defined")
        sys.exit(1)
    if not valid_account_id(log, account_id):
        log.critical("option '--invited-account-id' must be a valid account Id")
        sys.exit(1)
    if lookup(deployed_accounts, 'Id', account_id):
        log.error("account %s already in organization" % account_id)
        return
    invited_accounts = scan_invited_accounts(log, org_client)
    account_invite = [invite for invite in invited_accounts 
            if lookup(invite['Parties'], 'Type', 'ACCOUNT', 'Id') == account_id]
    if account_invite:
        log.debug('account_invite: %s' % account_invite)
        invite_state = account_invite[0]['State']
        log.debug('invite_state: %s' % invite_state)
        if invite_state == 'ACCEPTED':
            log.error('Account %s has already accepted a previous invite' % account_id)
            return
        if invite_state in ['REQUESTED', 'OPEN']:
            log.error('Account %s has already been invited to Org and status is %s' % (
                    account_id, invite_state))
            return
    log.info("inviting account %s to join Org" % account_id)
    if args['--exec']:
        target = dict(Id=account_id , Type='ACCOUNT')
        handshake = org_client.invite_account_to_organization(Target=target)['Handshake']
        log.info('account invite handshake Id: %s' % handshake['Id'])
        return handshake
    return


def display_invited_accounts(log, org_client):
    invited_accounts = scan_invited_accounts(log, org_client)
    if invited_accounts:
        header = "Invited Accounts in Org:"
        overbar = '_' * len(header)
        log.info("\n%s\n%s\n" % (overbar, header))
        fmt_str = "{:16}{:12}{}"
        log.info(fmt_str.format('Id:', 'State:', 'Expires:'))
        for invite in invited_accounts:
            account_id = lookup(invite['Parties'], 'Type', 'ACCOUNT', 'Id')
            invite_state = invite['State']
            invite_expiration = invite['ExpirationTimestamp']
            log.info(fmt_str.format(account_id, invite_state, invite_expiration))


def display_provisioned_accounts(log, deployed_accounts, status):
    """
    Print report of currently deployed accounts in AWS Organization.
    status::    matches account status (ACTIVE|SUSPENDED)
    """
    if status not in ('ACTIVE', 'SUSPENDED'):
        raise RuntimeError("'status' must be one of ('ACTIVE', 'SUSPENDED')")
    sorted_account_names = sorted([a['Name'] for a in deployed_accounts
            if a['Status'] == status])
    if sorted_account_names:
        header = '%s Accounts in Org:' % status.capitalize()
        overbar = '_' * len(header)
        log.info("\n%s\n%s\n" % (overbar, header))
        fmt_str = "{:20}{:20}{:16}{}"
        log.info(fmt_str.format('Name:', 'Alias', 'Id:', 'Email:'))
        for name in sorted_account_names:
            account = lookup(deployed_accounts, 'Name', name)
            log.info(fmt_str.format(
                    name,
                    account['Alias'],
                    account['Id'],
                    account['Email']))


def unmanaged_accounts(log, deployed_accounts, account_spec):
    deployed_account_names = [a['Name'] for a in deployed_accounts] 
    spec_account_names = [a['Name'] for a in account_spec['accounts']]
    log.debug('deployed_account_names: %s' % deployed_account_names)
    log.debug('spec_account_names: %s' % spec_account_names)
    return [a for a in deployed_account_names if a not in spec_account_names]


def main():
    args = docopt(__doc__, version=awsorgs.__version__)
    log = get_logger(args)
    log.debug(args)
    args = load_config(log, args)
    credentials = get_assume_role_credentials(
            args['--master-account-id'],
            args['--org-access-role'])
    if isinstance(credentials, RuntimeError):
        log.critical(credentials)
        sys.exit(1)
    org_client = boto3.client('organizations', **credentials)
    root_id = get_root_id(org_client)
    deployed_accounts = scan_deployed_accounts(log, org_client)

    if args['report']:
        aliases = get_account_aliases(log, deployed_accounts, args['--org-access-role'])
        deployed_accounts = merge_aliases(log, deployed_accounts, aliases)
        display_provisioned_accounts(log, deployed_accounts, 'ACTIVE')
        display_provisioned_accounts(log, deployed_accounts, 'SUSPENDED')
        display_invited_accounts(log, org_client)
        return

    account_spec = validate_spec(log, args)
    validate_master_id(org_client, account_spec)

    if args['create']:
        create_accounts(org_client, args, log, deployed_accounts, account_spec)
        unmanaged = unmanaged_accounts(log, deployed_accounts, account_spec)
        if unmanaged:
            log.warn("Unmanaged accounts in Org: %s" % (', '.join(unmanaged)))

    if args['alias']:
        queue_threads(log, deployed_accounts, set_account_alias,
                f_args=(log, args, account_spec, args['--org-access-role']),
                thread_count=10)

    if args['invite']:
        invite_account(log, args, org_client, deployed_accounts)
        

if __name__ == "__main__":
    main()
