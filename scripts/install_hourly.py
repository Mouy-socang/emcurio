"""安装/更新本机 Curiobject 每30分钟采集的 launchd 任务；不安装视觉任务或微信机器人。"""
import os,plistlib,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LABEL='com.curiobject.collect'

def configuration(root=ROOT):
    logs=root/'data/logs'
    return {'Label':LABEL,'ProgramArguments':[str(root/'.venv/bin/python'),'-m','curio.curio_monitor'],
            'WorkingDirectory':str(root),'StartInterval':1800,'RunAtLoad':False,
            'StandardOutPath':str(logs/'collect.log'),'StandardErrorPath':str(logs/'collect-error.log'),
            'EnvironmentVariables':{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONUNBUFFERED':'1'}}

def install():
    target=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
    target.parent.mkdir(parents=True,exist_ok=True)
    (ROOT/'data/logs').mkdir(parents=True,exist_ok=True)
    domain=f'gui/{os.getuid()}'
    loaded=subprocess.run(['launchctl','print',domain+'/'+LABEL],capture_output=True).returncode==0
    if loaded:subprocess.run(['launchctl','bootout',domain+'/'+LABEL],check=True)
    with target.open('wb') as f:plistlib.dump(configuration(),f)
    subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
    print('每30分钟采集任务已安装：'+LABEL)
    print('首次运行：launchctl kickstart '+domain+'/'+LABEL)

if __name__=='__main__':install()
