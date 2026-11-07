project('brick-captive-wpe', 'c', default_options: ['c_std=c11', 'warning_level=3'])

glib = dependency('glib-2.0')
gio = dependency('gio-2.0')
wpewebkit = dependency('wpe-webkit-2.0', version: '>=2.54.0')

executable('brick-captive-wpe',
  'brick-captive-wpe.c',
  'brick-controller.c',
  'brick-osk-context.c',
  'brick-osk-model.c',
  'captive-policy.c',
  dependencies: [glib, gio, wpewebkit],
  install: true,
)

policy_test = executable('test-captive-policy',
  'test-captive-policy.c',
  'captive-policy.c',
  dependencies: [glib],
)
test('captive-policy', policy_test)

osk_model_test = executable('test-osk-model',
  'test-osk-model.c',
  'brick-osk-model.c',
  dependencies: [glib],
)
test('osk-model', osk_model_test)
