const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const root = join(__dirname, '../src/k8s_stack_tooling/postgres');
const roles = [
  { role: 'readWriteAnyDatabase', db: 'admin' },
  { role: 'clusterAdmin', db: 'admin' },
];
const user = () => ({ user: 'synthetic_product', db: 'admin', _id: 'admin.synthetic_product', roles });

function provision(users, options = {}) {
  const mutations = [];
  const admin = {
    auth: () => true,
    runCommand: () => ({ ok: 1, users }),
    createUser: (value) => {
      mutations.push('create');
      users.push({ ...value, db: 'admin', _id: `admin.${value.user}` });
    },
    updateUser: () => mutations.push('update'),
  };
  const context = {
    db: { getSiblingDB: () => admin },
    process: { env: {
      DOCUMENTDB_USER: 'synthetic_product',
      DOCUMENTDB_ADMIN_USER: 'operations_admin',
      DOCUMENTDB_ADMIN_PASSWORD: 'synthetic-admin',
      DOCUMENTDB_PASSWORD: 'synthetic-app',
      DOCUMENTDB_ROLE_EXISTS: options.roleExists ?? (users.some(u => u.user === 'synthetic_product') ? 't' : 'f'),
      DOCUMENTDB_PREFLIGHT: options.preflight ? 'true' : 'false',
    } },
  };
  let error;
  try {
    vm.runInNewContext(readFileSync(join(root, 'provision.js'), 'utf8'), context);
  } catch (value) {
    error = value;
  }
  return { mutations, error };
}

test('synthetic product creation and established matching user update', () => {
  for (const [users, expected] of [[[], 'create'], [[user()], 'update']]) {
    const result = provision(users);
    assert.equal(result.error, undefined);
    assert.deepEqual(result.mutations, [expected]);
  }
  const checked = provision([user()], { preflight: true });
  assert.equal(checked.error, undefined);
  assert.deepEqual(checked.mutations, []);
});

test('conflicting identities and another consumer are rejected before mutation', () => {
  for (const [users, options] of [
    [[{ ...user(), roles: [{ role: 'root', db: 'admin' }] }], {}],
    [[{ ...user(), user: 'other_product' }], {}],
    [[], { roleExists: 't' }],
    [[user()], { roleExists: 'f' }],
  ]) {
    const result = provision(users, options);
    assert.ok(result.error);
    assert.deepEqual(result.mutations, []);
  }
});

test('failed readback deletes only the unique probe document', () => {
  const documents = new Map([['unrelated', { _id: 'unrelated', retained: true }]]);
  const collection = {
    insertOne: (value) => documents.set(value._id, value),
    findOne: () => null,
    deleteOne: (filter) => documents.delete(filter._id),
    drop: () => assert.fail('must not drop a collection'),
  };
  assert.throws(() => vm.runInNewContext(readFileSync(join(root, 'verify.js'), 'utf8'), {
    ObjectId: class { constructor() { this.synthetic = true; } },
    process: { env: { DOCUMENTDB_DATABASE: 'SyntheticProduct' } },
    db: { getSiblingDB: (name) => name === 'admin'
      ? { auth: () => true } : { getCollection: () => collection } },
  }), /read\/write verification failed/);
  assert.deepEqual([...documents.keys()], ['unrelated']);
});
