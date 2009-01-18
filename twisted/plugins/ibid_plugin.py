from zope.interface import implements

from twisted.python import usage
from twisted.plugin import IPlugin
from twisted.application.service import IServiceMaker, MultiService

import ibid

class Options(usage.Options):
    optParameters = [['config', 'c', 'ibid.ini', 'Configuration file to use']]

class IbidServiceMaker(object):
    implements(IServiceMaker, IPlugin)
    tapname = 'ibid'
    description = 'An extensible IRC/IM bot'
    options = Options

    def makeService(self, options):
        service = MultiService()
        ibid.setup(service, options['config'])
        return service

serviceMaker = IbidServiceMaker()

# vi: set et sta sw=4 ts=4: