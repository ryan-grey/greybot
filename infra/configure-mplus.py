"""Configure Mythic+ on an existing raid Lambda, preserving other settings.

Preview by default. Deploy the tested Lambda package first; --apply enables
collection, while --publish additionally enables the Tuesday recap schedule.
The existing recap schedule supplies the scheduler execution role.
"""
import argparse
import json

import boto3


def configure(args):
    session=boto3.Session(profile_name=args.profile,region_name=args.region)
    lam,sch,iam=(session.client(name) for name in ('lambda','scheduler','iam'))
    cfg=lam.get_function_configuration(FunctionName=args.function)
    template=sch.get_schedule(Name=args.template_schedule)
    table=cfg.get('Environment',{}).get('Variables',{}).get('STATE_TABLE')
    if not table or not args.channel.isdecimal():
        raise ValueError('An existing state table and numeric Discord channel are required')
    arn=cfg['FunctionArn'].split(':')
    partition,region,account=arn[1],arn[3],arn[4]
    role=cfg['Role'].split('/')[-1]
    policy={'Version':'2012-10-17','Statement':[{
        'Effect':'Allow','Action':'dynamodb:Query',
        'Resource':f'arn:{partition}:dynamodb:{region}:{account}:table/{table}',
        'Condition':{'ForAllValues:StringLike':{'dynamodb:LeadingKeys':['MPLUS#*']}}
    }]}
    schedules=[(args.function+'-mplus-collect','rate(1 minute)','UTC','mplus_collect',True),
               (args.function+'-mplus-records','rate(1 minute)','UTC','mplus_records',args.records),
               (args.function+'-mplus-recap','cron(0 10 ? * TUE *)','America/New_York','mplus_recap',args.publish)]
    print(json.dumps({'function':args.function,'query_scope':'MPLUS partition only',
                      'schedules':[{'name':n,'expression':e,'timezone':z,'enabled':on}
                                   for n,e,z,mode,on in schedules],'apply':args.apply}))
    if not args.apply:
        return
    iam.put_role_policy(RoleName=role,PolicyName='greybot-mplus-query',PolicyDocument=json.dumps(policy))
    env={**cfg.get('Environment',{}).get('Variables',{}),'MPLUS_ENABLED':'1',
         'MPLUS_CHANNEL_ID':args.channel,'MPLUS_SCORE_POLICY':'overall_for_participants'}
    if args.record_board:
        env['MPLUS_RECORD_BOARD_ENABLED']='1'
    if env != cfg.get('Environment',{}).get('Variables',{}):
        lam.update_function_configuration(FunctionName=args.function,RevisionId=cfg['RevisionId'],
                                          Environment={'Variables':env})
        lam.get_waiter('function_updated_v2').wait(FunctionName=args.function)
    for name,expression,zone,mode,enabled in schedules:
        desired={'Name':name,'ScheduleExpression':expression,'ScheduleExpressionTimezone':zone,
                 'FlexibleTimeWindow':{'Mode':'OFF'},'State':'ENABLED' if enabled else 'DISABLED',
                 'Target':{'Arn':cfg['FunctionArn'],'RoleArn':template['Target']['RoleArn'],
                           'Input':json.dumps({'mode':mode}),
                           'RetryPolicy':{'MaximumEventAgeInSeconds':300,'MaximumRetryAttempts':0}}}
        try:
            previous=sch.get_schedule(Name=name)
        except sch.exceptions.ResourceNotFoundException:
            sch.create_schedule(**desired)
        else:
            # Preserve any explicitly configured notification/encryption metadata.
            for field in ('Description','KmsKeyArn','ActionAfterCompletion'):
                if field in previous:desired[field]=previous[field]
            if previous['Target'].get('DeadLetterConfig'):
                desired['Target']['DeadLetterConfig']=previous['Target']['DeadLetterConfig']
            sch.update_schedule(**desired)
        actual=sch.get_schedule(Name=name)
        assert all(actual[k]==desired[k] for k in ('ScheduleExpression','ScheduleExpressionTimezone','State','Target'))
        print(name+': verified '+actual['State'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--region',default='us-east-1')
    parser.add_argument('--function',required=True)
    parser.add_argument('--template-schedule',required=True)
    parser.add_argument('--channel',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--records',action='store_true',help='Enable new dungeon record announcements')
    parser.add_argument('--record-board',action='store_true',help='Maintain a pinned dungeon-record card')
    configure(parser.parse_args())
