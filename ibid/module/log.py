import time

import ibid
from ibid.module import Module
from ibid.decorators import *

class Log(Module):

	def __init__(self, name):
		Module.__init__(self, name)
		self.log = open(ibid.core.config['modules'][self.name]['logfile'], 'a')

	@message
	def process(self, query):
		now = time.strftime(u"%Y/%m/%d %H:%M:%S", time.localtime())
		self.log.write(u'%s %s: %s > %s: %s\n' % (now, query['source'], query['user'], query['channel'], query['msg']))
		for response in query['responses']:
			self.log.write(u'%s %s: me > %s: %s\n' % (now, query['source'], response['target'], response['reply']))
		self.log.flush()