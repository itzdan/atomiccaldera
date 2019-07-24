import asyncio, json, logging, os, sys, re, uuid, yaml

from plugins.atomiccaldera.app.artyaml import ARTyaml
from app.utility.logger import Logger

from base64 import b64encode, b64decode
from aiohttp import web
from aiohttp_jinja2 import template
from stix2 import FileSystemSource
from stix2 import Filter

class AtomicCaldera:

	def __init__(self, services, ac_data_svc):
		self.ac_data_svc = ac_data_svc
		self.data_svc = services.get('data_svc')
		self.auth_svc = services.get('auth_svc')
		self.log = Logger('atomiccaldera')
		self.log.debug('Atomic-Caldera Plugin Logging started.')
		self.get_conf()
		self.fs = FileSystemSource(self.ctipath)

	def get_conf(self):
		confPath = os.path.join(os.path.dirname(os.path.realpath(__file__)), '../conf/artconf.yml')
		try:
			with open(confPath, 'r') as c:
				conf = yaml.load(c, Loader=yaml.Loader)
			self.ctipath = os.path.expanduser(os.path.join(conf['ctipath'], 'enterprise-attack/'))
			self.artpath = os.path.expanduser(conf['artpath'])
			self.log.debug(self.ctipath)
			self.log.debug(self.artpath)
		except:
			pass

	@template('atomiccaldera.html')
	async def landing(self, request):
		await self.auth_svc.check_permissions(request)
		abilities = []
		tactics = []
		variables = []
		try:
			abilities = await self.ac_data_svc.explode_art_abilities()
			for ab in abilities:
				if not ab['tactic'] in tactics:
					tactics.append(ab['tactic'])
		except Exception as e:
			self.log.error(e)

		try:
			variables = await self.ac_data_svc.explode_art_variables()
		except Exception as e:
			self.log.error(e)
		return { 'abilities': json.dumps(abilities), 'tactics': tactics, 'variables': json.dumps(variables) }

	async def getMITREPhase(self, attackID):
		filter = [
					Filter('type', '=', 'attack-pattern'),
					Filter('external_references.external_id', '=', attackID)
				]
		result = self.fs.query(filter)
		if result:
			return result[0].kill_chain_phases[0].phase_name
		else:
			return 'unknown'

	async def get_atomics(self):
		await self.ac_data_svc.build_db(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../conf/ac.sql'))
		artAbilities = []
		artVars = []
		if os.path.exists(self.artpath):
			for root, dirs, files in os.walk(self.artpath):
				for procFile in files:
					fullFile = os.path.join(root, procFile)
					if os.path.splitext(fullFile)[-1].lower() == '.yaml':
						self.log.debug('Processing {}'.format(fullFile))
						try:
							artObj = ARTyaml()
						except:
							continue
						with open(fullFile, 'r') as yamlFile:
							try:
								artObj.load(yamlFile)
							except:
								continue
						# Loop through the tests
						if artObj.atomicTests:
							for atomic in artObj.atomicTests:
								name = atomic['name']
								description = atomic['description']
								if 'command' in atomic['executor'].keys():
									command = re.sub(r'x07', r'a', repr(atomic['executor']['command'])).strip()
									command = command.encode('utf-8').decode('unicode_escape')
									executor = atomic['executor']['name']
									if command[0] == '\'':
										command = command.strip('\'')
									elif command[0] == '\"':
										command = command.strip('\"')
								else:
									command = ''
									executor = ''
								
								try:
									if command != '':
										checkUnique = { 'technique': int(artObj.attackTech[1:]),
											'command': b64encode(command.encode('utf-8')).decode('utf-8')}
								except Exception as e:
									print(e)
								
								# Check to see if the command has been added to the database
								if (command != '' and not await self.ac_data_svc.check_art_ability(checkUnique)):
									uuidBool = True
									while(uuidBool):
										ability_id = str(uuid.uuid4())
										if not await self.ac_data_svc.check_art_ability({ 'ability_id': ability_id }):
											uuidBool = False

									try:
										# Add the new ability to export
										artAbilities.append({'ability_id': ability_id,
											'technique': artObj.attackTech[1:],
											'name': name,
											'description': description,
											'tactic': await self.getMITREPhase(artObj.attackTech),
											'attack_name': artObj.displayName,
											'executor': executor,
											'command': b64encode(command.encode('utf-8')).decode('utf-8')})
									except Exception as e:
										print(e)

									if 'input_arguments' in atomic.keys():
										for argument in atomic['input_arguments'].keys():
											try:
												curVar = re.sub(r'x07', r'a', repr(atomic['input_arguments'][argument]['default'])).strip()
												if curVar[0] == '\'':
													curVar = curVar.strip('\'')
												elif curVar[0] == '\"':
													curVar = curVar.strip('\"')
												artVars.append({'ability_id': ability_id,
													'var_name': argument,
													'value': b64encode(curVar.encode('utf-8')).decode('utf-8')})
											except:
												pass
		else:
			self.log.debug('Paths are not valid')
			return {'abilities': [], 'variables': []}
		self.log.debug('Got to the end.')
		return {'abilities': artAbilities, 'variables': artVars}

	async def get_art(self, request):
		self.log.debug('Landed in get_art.')
		try:
			atomics = await self.get_atomics()
		except Exception as e:
			self.log.error(e)
			pass
		return atomics

	async def import_art_abilities(self):
		try:
			atomics = await self.get_atomics()
		except Exception as e:
			self.log.error(e)
			return 'Failed to load abilities.'
		for ability in atomics['abilities']:
			await self.ac_data_svc.create_art_ability(ability)
		for variable in atomics['variables']:
			await self.ac_data_svc.create_art_variable(variable)
		return 'Successfully imported new abilities.'
	
	async def save_art_ability(self, data):
		key = data.pop('key')
		value = data.pop('value')
		updates = data.pop('data')
		if await self.ac_data_svc.update_art_ability(key, value, updates):
			return 'Updated ability: {}'.format(value)
		else:
			return 'Update failed for ability: {}'.format(value)

	async def save_art_variables(self, data):
		key = data.pop('key')
		value = data.pop('value')
		updates = data.pop('data')
		if await self.ac_data_svc.update_art_variables(key, value, updates):
			return 'Updated variables successfully.'
		else:
			return 'Updates to variables failed.'

	async def rest_api(self, request):
		self.log.debug('Starting Rest call.')
		await self.auth_svc.check_permissions(request)
		data = dict(await request.json())
		index = data.pop('index')
		self.log.debug('Index: {}'.format(index))
		
		options = dict(
			PUT=dict(
				ac_ability=lambda d: self.import_art_abilities(**d)
			),
			POST=dict(
				ac_ability=lambda d: self.ac_data_svc.explode_art_abilities(**d),
				ac_ability_save=lambda d: self.save_art_ability(data=d),
				ac_variables_save=lambda d: self.save_art_variables(data=d)
			),
			DELETE=dict(
				delete_all=lambda d: self.ac_data_svc.delete_all(**d)
			)
		)
		try:
			output = await options[request.method][index](data)
		except Exception as e:
			self.log.error(e)
		return web.json_response(output)

