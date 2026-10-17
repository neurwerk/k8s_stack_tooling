const admin = db.getSiblingDB("admin");
if (!admin.auth(process.env.DOCUMENTDB_ADMIN_USER, process.env.DOCUMENTDB_ADMIN_PASSWORD)) {
  throw new Error("DocumentDB administrator authentication failed");
}
const username = process.env.DOCUMENTDB_USER;
const roles = [
  { role: "readWriteAnyDatabase", db: "admin" },
  { role: "clusterAdmin", db: "admin" },
];
function rolesMatch(actual) {
  return Array.isArray(actual) && actual.length === roles.length
    && roles.every((expected) => actual.some(
      (role) => role.role === expected.role && role.db === expected.db,
    ));
}
function inventory() {
  const result = admin.runCommand({ usersInfo: 1 });
  if (result.ok !== 1 || !Array.isArray(result.users)) {
    throw new Error("DocumentDB user inventory failed");
  }
  // These broad roles permit exactly one product on this shared instance.
  if (result.users.some((user) => ![
    process.env.DOCUMENTDB_ADMIN_USER, username,
  ].includes(user.user) || user.db !== "admin")) {
    throw new Error("DocumentDB already has another application consumer");
  }
  return result.users.find((user) => user.user === username) ?? null;
}
const existing = inventory();
if (existing !== null && (existing._id !== `admin.${username}` || !rolesMatch(existing.roles))) {
  throw new Error("DocumentDB application user role verification failed");
}
if ((existing !== null) !== (process.env.DOCUMENTDB_ROLE_EXISTS === "t")) {
  throw new Error("DocumentDB application identity is incomplete or conflicting");
}
if (process.env.DOCUMENTDB_PREFLIGHT === "true") {
  // Authentication and inventory only, before any temporary elevation or mutation.
} else if (existing === null) {
  admin.createUser({ user: username, pwd: process.env.DOCUMENTDB_PASSWORD, roles });
} else {
  admin.updateUser(username, { pwd: process.env.DOCUMENTDB_PASSWORD });
}
const configured = inventory();
if (process.env.DOCUMENTDB_PREFLIGHT !== "true" &&
    (configured === null || configured._id !== `admin.${username}` || !rolesMatch(configured.roles))) {
  throw new Error("DocumentDB application user verification failed");
}
