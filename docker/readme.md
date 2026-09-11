### Создать тестового пользователя в LDAP

cat <<EOF | docker exec -i ldap ldapadd -x -D "cn=admin,dc=example,dc=com" -w adminpass
dn: ou=users,dc=example,dc=com
objectClass: organizationalUnit
ou: users

dn: uid=testuser,ou=users,dc=example,dc=com
objectClass: inetOrgPerson
objectClass: posixAccount
objectClass: shadowAccount
uid: testuser
cn: Test User
sn: User
givenName: Test
mail: testuser@example.com
userPassword: {SHA}W6ph5Mm5Pz8GgiULbPgzG37mj9g=
uidNumber: 1000
gidNumber: 1000
homeDirectory: /home/testuser
loginShell: /bin/bash
EOF
