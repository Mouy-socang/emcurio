"""Install the explicitly authorized continuous visual collector."""
import os,plistlib,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LABEL='com.curio.visual.phase1'
def configuration(root=ROOT):
 return {'Label':LABEL,'ProgramArguments':[str(root/'.venv/bin/python'),'-m','curio.vision_worker'],'WorkingDirectory':str(root),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':60,'StandardOutPath':str(root/'data/porcelain/worker.log'),'StandardErrorPath':str(root/'data/porcelain/worker-error.log'),'EnvironmentVariables':{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','PYTHONUNBUFFERED':'1'}}
def install():
 (ROOT/'data/porcelain').mkdir(parents=True,exist_ok=True)
 domain='gui/'+str(os.getuid());target=Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
 if subprocess.run(['launchctl','print',domain+'/'+LABEL],capture_output=True).returncode==0:subprocess.run(['launchctl','bootout',domain+'/'+LABEL],check=True)
 target.parent.mkdir(parents=True,exist_ok=True)
 with target.open('wb') as f:plistlib.dump(configuration(),f)
 subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
 print('持续视觉任务已安装：'+LABEL)
if __name__=='__main__':install()
