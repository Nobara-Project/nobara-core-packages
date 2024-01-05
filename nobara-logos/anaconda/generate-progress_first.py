#!/usr/bin/python

import os
import xml.dom.minidom
import sys


def get_a_document(doc="progress-first.svg"):
	return xml.dom.minidom.parse(doc)

def find_version_nodes(doc):
	version_nodes = []
	tspan_nodes = doc.getElementsByTagName('tspan')
        j = 0
	for tspan in tspan_nodes:
		if tspan.attributes["id"].value.find("version") >= 0:
#               version_nodes.append(tspan.attributes["id"].value)
                	version_nodes.append(tspan)
	return version_nodes

def replace_version_number(version,version_nodes):
	for node in version_nodes:
#		print node.childNodes[0].nodeValue
		node.childNodes[0].nodeValue = version
#		print "Current node is " + node.childNodes[0].nodeValue

def save_document(doc,version):
	name = "progress-first-" + version + ".svg"
        f = open(name, "w");
        f.write(doc.toxml(encoding="utf8"));
        f.close()
#	xml.dom.ext.PrettyPrint(doc, open(name, "w"))

print "What's the version of Fedora? : "
version = sys.stdin.readline()[:-1]
# print "Version is " + version
doc = get_a_document()
version_nodes = find_version_nodes(doc)
replace_version_number(version,version_nodes)
save_document(doc,version)
filename = "progress-first-" + version
os.system("inkscape -C %s -e %s" % (filename + ".svg",filename + ".png"))
print "\nYour updated Fedora progress first PNG has been output to: "
print "\n" + filename + ".png"
