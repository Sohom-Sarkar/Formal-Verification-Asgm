// ISCAS-85 c17: the classic six-NAND benchmark, in its original gate-level
// form. Small, but it is the standard smoke test for equivalence tools.
module c17(input N1, input N2, input N3, input N6, input N7,
           output N22, output N23);
  wire N10, N11, N16, N19;

  nand NAND2_1(N10, N1,  N3);
  nand NAND2_2(N11, N3,  N6);
  nand NAND2_3(N16, N2,  N11);
  nand NAND2_4(N19, N11, N7);
  nand NAND2_5(N22, N10, N16);
  nand NAND2_6(N23, N16, N19);
endmodule
