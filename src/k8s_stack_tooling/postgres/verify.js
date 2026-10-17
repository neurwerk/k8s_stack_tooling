const admin = db.getSiblingDB("admin");
if (!admin.auth(process.env.DOCUMENTDB_USER, process.env.DOCUMENTDB_PASSWORD)) {
  throw new Error("DocumentDB application authentication failed");
}
const target = db.getSiblingDB(process.env.DOCUMENTDB_DATABASE);
const collection = target.getCollection("__neurwerk_provisioning_verification");
const marker = new ObjectId();
try {
  collection.insertOne({ _id: marker, verified: true });
  const configured = collection.findOne({ _id: marker });
  if (configured === null || configured.verified !== true) {
    throw new Error("DocumentDB application read/write verification failed");
  }
} finally {
  collection.deleteOne({ _id: marker });
}
