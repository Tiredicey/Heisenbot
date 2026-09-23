module.exports = { apps: [{ name: 'heisenbot', script: 'python3', args: '-m heisenbot ui --no-open --port 8765', cwd: '/home/user/webapp', interpreter: 'none', env: { HEISENBOT_HOST: '0.0.0.0' } }] }
